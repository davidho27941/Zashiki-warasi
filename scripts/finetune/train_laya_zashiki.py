"""Single-GPU RLCD fine-tune of laya's multilingual checkpoint on the
Zashiki email-category task (15 effective labels — calendar merged).

Adapted from the official 2xT4 Kaggle notebook
(NandhaKishorM/laya notebooks/laya_finetune_typed_decisions_2xT4_kaggle
.ipynb): DDP stripped, dataset swapped for data/train.jsonl, one-hot
gold targets, question built from the repo's questions.json via
zashiki_laya_common (train == serve by construction).

Run on nttu-gpu-lab (RTX 4090). VRAM ladder if OOM beside llama.cpp:
--micro-batch 4 → 2 → pause llama.cpp for the run. See RUNBOOK.md.

    python train_laya_zashiki.py \
        --train train.jsonl --questions questions.json \
        --output laya_zashiki_v1 [--epochs 4] [--micro-batch 4]

Output dir: model.safetensors + encoder/ + tokenizer/ +
rl_agent_config.json — loadable by `laya.agent.load(<dir>)`.
PRIVACY: train data holds real mail text; everything stays on this box.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import time

import torch
from safetensors.torch import load_file, save_file
from transformers import AutoTokenizer

from laya.agent import _fix_tokenizer_config
from laya.common import QTYPES, build_model, build_sequence, proper_reward

from zashiki_laya_common import load_pairs, load_questions


def build_items(pairs, question, tok, cfg):
    """One training item per email: one-hot target on the gold label."""
    keys = list(question["criteria"].keys())
    idx = {k: i for i, k in enumerate(keys)}
    items, dropped = [], 0
    for p in pairs:
        target = [0.0] * len(keys)
        target[idx[p["en_label"]]] = 1.0
        seq, markers = build_sequence(
            tok,
            p["input_text"],
            {"t": "choice", "ins": question["instructions"],
             "crit": question["criteria"]},
            cfg["max_len"],
            cfg["head_max_len"],
        )
        if len(markers) != len(keys):
            dropped += 1
            continue
        items.append({
            "ids": seq, "markers": markers,
            "qtype": QTYPES["choice"],
            "target": target, "label": idx[p["en_label"]],
        })
    if dropped:
        print(f"dropped {dropped} items (marker/label-count mismatch)")
    return items


def collate(items, pad_id):
    n = len(items)
    L = max(len(it["ids"]) for it in items)
    kmax = max(len(it["markers"]) for it in items)
    ids = torch.full((n, L), pad_id, dtype=torch.long)
    att = torch.zeros((n, L), dtype=torch.long)
    mpos = torch.zeros((n, kmax), dtype=torch.long)
    mmask = torch.zeros((n, kmax), dtype=torch.bool)
    target = torch.zeros((n, kmax), dtype=torch.float32)
    for i, it in enumerate(items):
        ids[i, : len(it["ids"])] = torch.tensor(it["ids"])
        att[i, : len(it["ids"])] = 1
        k = len(it["markers"])
        mpos[i, :k] = torch.tensor(it["markers"])
        mmask[i, :k] = True
        target[i, : len(it["target"])] = torch.tensor(
            it["target"], dtype=torch.float32)
    return {
        "input_ids": ids, "attention_mask": att,
        "marker_pos": mpos, "marker_mask": mmask, "target": target,
        "qtype": torch.tensor([it["qtype"] for it in items]),
    }


def fit_one_temp(sel):
    if len(sel) < 10:
        return 1.0
    kmax = max(len(z) for z, _ in sel)
    Z = torch.full((len(sel), kmax), -1e4)
    T = torch.zeros((len(sel), kmax))
    for i, (z, t) in enumerate(sel):
        Z[i, : len(z)] = torch.tensor(z)
        T[i, : len(t)] = torch.tensor(t, dtype=torch.float32)
    log_t = torch.zeros(1, requires_grad=True)
    opt = torch.optim.LBFGS([log_t], lr=0.1, max_iter=100)

    def closure():
        opt.zero_grad()
        loss = -(T * torch.log_softmax(Z / log_t.exp(), -1)).sum(-1).mean()
        loss.backward()
        return loss

    opt.step(closure)
    return float(torch.clamp(log_t.exp(), 0.1, 10.0).item())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", default="train.jsonl")
    ap.add_argument("--questions", default="questions.json")
    ap.add_argument("--output", default="laya_zashiki_v1")
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--micro-batch", type=int, default=4)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--group-size", type=int, default=4)
    ap.add_argument("--lr-encoder", type=float, default=2.5e-5)
    ap.add_argument("--lr-head", type=float, default=1.0e-4)
    ap.add_argument("--seed", type=int, default=20260926)
    args = ap.parse_args()

    assert torch.cuda.is_available(), "CUDA required"
    device = torch.device("cuda", 0)

    from huggingface_hub import snapshot_download
    print("fetching base checkpoint (multilingual)...")
    root = snapshot_download("convaiinnovations/laya",
                             allow_patterns=["multilingual/*"])
    model_dir = os.path.join(root, "multilingual")
    _fix_tokenizer_config(model_dir)

    with open(os.path.join(model_dir, "rl_agent_config.json")) as f:
        cfg = json.load(f)
    cfg["gradient_checkpointing"] = True
    cfg["max_len"] = 1024
    cfg["head_max_len"] = 256

    tok = AutoTokenizer.from_pretrained(os.path.join(model_dir, "tokenizer"))
    question, _zh_by_en, en_by_zh = load_questions(args.questions)
    print(f"{len(question['criteria'])} effective labels "
          f"(calendar merged): {list(question['criteria'])}")

    pairs = load_pairs(args.train, en_by_zh)
    items = build_items(pairs, question, tok, cfg)
    print(f"{len(items)} training items from {len(pairs)} pairs")

    model = build_model(cfg, encoder_dir=os.path.join(model_dir, "encoder"))
    model.load_state_dict(
        load_file(os.path.join(model_dir, "model.safetensors")), strict=True)
    model.encoder.gradient_checkpointing_enable(
        gradient_checkpointing_kwargs={"use_reentrant": False})
    model.head_checkpointing = True
    model.to(device)
    model.train()

    # Calibration hold-out BEFORE training (same rationale as upstream:
    # temps fitted on trained-on items are degenerate).
    order = list(range(len(items)))
    random.Random(args.seed).shuffle(order)
    n_calib = min(400, len(items) // 10)
    calib_items = [items[i] for i in sorted(order[:n_calib])]
    train_items = [items[i] for i in sorted(order[n_calib:])]

    enc = [p for n, p in model.named_parameters() if "encoder." in n]
    head = [p for n, p in model.named_parameters() if "encoder." not in n]
    optimizer = torch.optim.AdamW(
        [{"params": enc, "lr": args.lr_encoder},
         {"params": head, "lr": args.lr_head}],
        weight_decay=0.01)
    total_updates = (len(train_items)
                     // (args.micro_batch * args.grad_accum)) * args.epochs
    scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(
        optimizer, T_max=max(1, total_updates), eta_min=1e-6)
    scaler = torch.amp.GradScaler("cuda", enabled=True)

    SIGMA_START, SIGMA_END = 0.4, 0.1
    print(f"training: {len(train_items)} items "
          f"({n_calib} held for calibration), {args.epochs} epochs, "
          f"micro={args.micro_batch} accum={args.grad_accum}")
    t0 = time.time()

    for epoch in range(args.epochs):
        random.seed(42 + epoch)
        random.shuffle(train_items)
        epoch_loss, n_batches, accum = 0.0, 0, 0
        optimizer.zero_grad(set_to_none=True)
        sigma = SIGMA_START + (SIGMA_END - SIGMA_START) * (
            epoch / max(1, args.epochs - 1))

        for b in range(0, len(train_items), args.micro_batch):
            chunk = train_items[b: b + args.micro_batch]
            if not chunk:
                continue
            batch = collate(chunk, tok.pad_token_id)
            with torch.autocast("cuda", dtype=torch.float16):
                logits, act = model(
                    batch["input_ids"].to(device),
                    batch["attention_mask"].to(device),
                    batch["marker_pos"].to(device),
                    batch["marker_mask"].to(device),
                    batch["qtype"].to(device))
            logits = logits.float()
            mask = batch["marker_mask"].to(device)
            k = mask.sum(-1, keepdim=True).float()
            target = batch["target"].to(device)

            eps = torch.randn((args.group_size,) + logits.shape,
                              device=device) * sigma * mask
            eps = (eps - eps.sum(-1, keepdim=True) / k) * mask
            z = logits.detach().unsqueeze(0) + eps
            q = torch.softmax(z.masked_fill(~mask, -1e4), -1)
            with torch.no_grad():
                r = proper_reward(q, target.unsqueeze(0),
                                  batch["qtype"].to(device), mask,
                                  w_sph=0.75, w_rps=1.0)
                adv = r - r.mean(0, keepdim=True)
                adv = adv / (adv.std() + 1e-6)
            logp = -(((z - logits.unsqueeze(0)) ** 2) * mask).sum(-1) / (
                2 * sigma ** 2)
            loss_rl = -(adv * logp).mean()
            loss_ce = -(target * torch.log_softmax(
                logits.masked_fill(~mask, -1e4), -1)).sum(-1).mean()
            loss = (loss_rl + loss_ce) / args.grad_accum + 0.0 * act.sum()

            scaler.scale(loss).backward()
            accum += 1
            if accum % args.grad_accum == 0 or (
                    b + args.micro_batch) >= len(train_items):
                scaler.unscale_(optimizer)
                torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
                scaler.step(optimizer)
                scaler.update()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            epoch_loss += loss.item() * args.grad_accum
            n_batches += 1
            if n_batches % 50 == 0:
                print(f"  ep{epoch + 1}/{args.epochs} step {n_batches} "
                      f"loss {loss.item() * args.grad_accum:.4f} "
                      f"reward {r.mean().item():.3f} "
                      f"lr {scheduler.get_last_lr()[0]:.2e}")

        print(f"=== epoch {epoch + 1}/{args.epochs} "
              f"{time.time() - t0:.0f}s avg-loss "
              f"{epoch_loss / max(1, n_batches):.4f} ===")
        ckpt = os.path.join(args.output, "checkpoint_latest")
        os.makedirs(ckpt, exist_ok=True)
        save_file({kk: v.half().contiguous().cpu()
                   for kk, v in model.state_dict().items()},
                  os.path.join(ckpt, "model.safetensors"))

    # Temperature refit on the held-out slice
    print("fitting calibration temperature...")
    del optimizer, scaler, scheduler
    torch.cuda.empty_cache()
    model.eval()
    calib_preds = []
    with torch.no_grad():
        for c in range(0, len(calib_items), 16):
            cb = collate(calib_items[c: c + 16], tok.pad_token_id)
            with torch.autocast("cuda", dtype=torch.float16):
                l_sub, _ = model(
                    cb["input_ids"].to(device),
                    cb["attention_mask"].to(device),
                    cb["marker_pos"].to(device),
                    cb["marker_mask"].to(device),
                    cb["qtype"].to(device))
            l_np = l_sub.float().cpu().numpy()
            for r_i, it in enumerate(calib_items[c: c + 16]):
                kk = len(it["markers"])
                calib_preds.append((l_np[r_i, :kk], it["target"]))
    temps = [1.2, 1.2, 1.2]
    try:
        temps[QTYPES["choice"]] = fit_one_temp(calib_preds)
    except Exception as exc:  # noqa: BLE001
        print("temp fit fallback:", exc)
    print("fitted temps (choice slot):", [round(t, 3) for t in temps])

    os.makedirs(args.output, exist_ok=True)
    save_file({kk: v.half().contiguous().cpu()
               for kk, v in model.state_dict().items()},
              os.path.join(args.output, "model.safetensors"))
    model.encoder.config.save_pretrained(
        os.path.join(args.output, "encoder"))
    tok.save_pretrained(os.path.join(args.output, "tokenizer"))
    cfg["fine_tuned"] = True
    cfg["model_name"] = "laya-zashiki-email-v1"
    cfg["temperature"] = temps
    cfg.pop("temperature_by_options", None)
    with open(os.path.join(args.output, "rl_agent_config.json"), "w") as f:
        json.dump(cfg, f, indent=2)
    print(f"saved fine-tuned checkpoint to {args.output}")


if __name__ == "__main__":
    main()
