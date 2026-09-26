# laya fine-tune on nttu-gpu-lab (RTX 4090) — task 0.4 runbook

Everything runs on the 4090 box. Training data holds real mail text —
it never leaves the LAN (design D10: privacy is why we're not on Kaggle).

## 1. Ship files from the laptop (run ON THE LAPTOP)

```bash
cd ~/workplace/homelab/Zashiki-warasi
ssh david@192.168.1.254 'mkdir -p ~/laya-finetune'
scp scripts/finetune/*.py \
    deploy/helm/laya-classifier/configs/questions.json \
    data/train.jsonl data/test.jsonl \
    david@192.168.1.254:~/laya-finetune/
```

## 2. Environment (run ON nttu-gpu-lab, once)

```bash
cd ~/laya-finetune
python3 -m venv venv
source venv/bin/activate
pip install -U pip
pip install "laya==0.3.20" "transformers>=4.48.0" safetensors huggingface_hub
# CUDA torch: laya pulls torch; verify it sees the GPU —
python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
# If cuda.is_available() is False, install the CUDA wheel explicitly:
#   pip install torch --index-url https://download.pytorch.org/whl/cu124
```

## 3. VRAM check (llama.cpp holds ~18.4 GB of 24 GB)

```bash
nvidia-smi --query-gpu=memory.used,memory.free --format=csv,noheader
```

Ladder — try in order, move down only on OOM:
1. `--micro-batch 4 --grad-accum 8` beside llama.cpp (needs ~5 GB — tight)
2. `--micro-batch 2 --grad-accum 16`
3. Pause llama.cpp for the run (email pipeline degrades to
   AnalysisFailed alerts for those hours — pick a night window):
   `sudo systemctl stop <llama-cpp-unit>` … train … `start` again.

## 4. Train (~1-3 h expected for 3.9k items × 4 epochs on a 4090)

```bash
cd ~/laya-finetune && source venv/bin/activate
nohup python train_laya_zashiki.py \
    --train train.jsonl --questions questions.json \
    --output laya_zashiki_v1 --micro-batch 4 --grad-accum 8 \
    > train.log 2>&1 &
tail -f train.log          # Ctrl-C detaches from tail only
```

Rolling checkpoint lands in `laya_zashiki_v1/checkpoint_latest/` after
every epoch — a crash never loses more than one epoch.

## 5. Evaluate — the D10 gate

```bash
# Fine-tuned:
python eval_laya_zashiki.py --model laya_zashiki_v1 \
    --test test.jsonl --questions questions.json --report eval_tuned.json
# Zero-shot baseline for the delta (optional but nice for the record):
python eval_laya_zashiki.py --model base \
    --test test.jsonl --questions questions.json --report eval_base.json
```

Gate: **overall ≥85% AND no class <60% (n≥5) → GO**; below → paste
`eval_tuned.json` back into the Claude session and we iterate
(criteria wording, epochs, or data curation) or call NO-GO.

## 6. Ship results back (run ON THE LAPTOP)

```bash
scp david@192.168.1.254:~/laya-finetune/eval_*.json data/
scp david@192.168.1.254:~/laya-finetune/train.log data/
# The checkpoint stays on the 4090 until the gate passes; if GO:
scp -r david@192.168.1.254:~/laya-finetune/laya_zashiki_v1 data/
```

Then report back in the Claude session with `eval_tuned.json`'s content.

## Notes

- `questions.json` is consumed through `zashiki_laya_common.py`, which
  merges `event_info` + `meeting_invite` into `calendar_event` (15
  effective labels) — Level-1 taxonomy per the openspec two-level
  design. Train, eval, and the future serve path all share this
  construction.
- First run downloads the multilingual base checkpoint from HF Hub
  (~few hundred MB) into `~/.cache/huggingface`.
- If the box lacks internet for HF: copy the laptop's cache dir
  `~/.cache/huggingface/hub/models--convaiinnovations--laya` across.
