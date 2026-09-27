# laya-serve image (fine-tuned zashiki checkpoint)

## One-time: upload a checkpoint to the GitLab generic package registry

From wherever the trained checkpoint lives (currently nttu-gpu-lab):

```bash
cd ~/zashiki-warasi
tar czf laya_zashiki_v1.tar.gz laya_zashiki_v1/
curl --header "PRIVATE-TOKEN: <PAT with api scope>" \
     --upload-file laya_zashiki_v1.tar.gz \
     "https://gitlab.davidho.dev/api/v4/projects/34/packages/generic/laya-checkpoint/v1/laya_zashiki_v1.tar.gz"
```

The registry is private (repo-scoped auth) — the weights were trained
on personal mail and must never reach the public mirror; the package
registry and the container registry are both inside that boundary.

Retraining later → upload as `.../laya-checkpoint/v2/laya_zashiki_v2.tar.gz`
and bump `LAYA_CHECKPOINT_VERSION` when triggering the build job.

## Build: manual CI job

`Build_Laya_Image` in `.gitlab-ci.yml` is `when: manual` (the checkpoint
changes rarely; no point building 2 GB on every push). Trigger it from
the pipeline UI; it downloads the checkpoint with the job's own
`CI_JOB_TOKEN`, bakes it, and pushes
`gitlab.davidho.dev:5050/homelab/zashiki-warasi/laya-serve:<version>`.

## Local build (fallback, e.g. registry quirks)

```bash
cd deploy/docker/laya-serve
mkdir -p checkpoint && tar xzf /path/to/laya_zashiki_v1.tar.gz -C checkpoint --strip-components=1
docker build -t gitlab.davidho.dev:5050/homelab/zashiki-warasi/laya-serve:v1 .
docker push gitlab.davidho.dev:5050/homelab/zashiki-warasi/laya-serve:v1
```

## Smoke (task 1.4 — no-network start proves weights are baked)

```bash
docker run --rm --network=none -p 8000:8000 gitlab.davidho.dev:5050/homelab/zashiki-warasi/laya-serve:v1 &
sleep 30 && curl -s localhost:8000/health
curl -s -X POST localhost:8000/v1/systemone -H 'content-type: application/json' \
  -d '{"state":"限時三天全站 8 折!結帳輸入 SAVE20","questions":{"category":{"type":"choice","instructions":"Which category?","criteria":{"promotion":"a discount offer","other":"none"}}}}'
```
