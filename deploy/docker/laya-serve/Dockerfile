# laya-serve with the fine-tuned zashiki checkpoint baked in.
#
# Build context expects `checkpoint/` populated (CI downloads it from
# the GitLab generic package registry; see .gitlab-ci.yml
# Build_Laya_Image). The checkpoint holds weights trained on the
# operator's personal mail — this image goes ONLY to the private
# registry, never anywhere public.
#
# torch is installed from the CPU index FIRST: the default PyPI wheel
# bundles CUDA (5+ GB); the CPU wheel keeps the image ~2 GB total.

FROM python:3.12-slim

ENV PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1

RUN pip install torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install "laya==0.3.20" fastapi uvicorn

COPY checkpoint/ /model/
COPY serve_zashiki.py /app/serve_zashiki.py

# _fix_tokenizer_config edits files inside the model dir when needed;
# run it at BUILD time so the runtime filesystem can stay read-only.
RUN python -c "from laya.agent import _fix_tokenizer_config; _fix_tokenizer_config('/model')"

ENV LAYA_MODEL_PATH=/model \
    LAYA_MODEL_NAME=laya-zashiki-v1 \
    LAYA_HOST=0.0.0.0 \
    LAYA_PORT=8000 \
    LAYA_THREADS=2

EXPOSE 8000
CMD ["python", "/app/serve_zashiki.py"]
