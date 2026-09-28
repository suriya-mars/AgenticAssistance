# Same result as ollama.Dockerfile, but the models are DOWNLOADED during the build (needs internet),
# so the build machine does not need Ollama installed. Used by scripts/build.sh when models aren't local.
# Behind a corporate proxy, Docker passes HTTP_PROXY / HTTPS_PROXY build args through automatically.
ARG OLLAMA_VERSION=0.34.4
FROM ollama/ollama:${OLLAMA_VERSION} AS dist
RUN cp -a /usr/lib/ollama /opt/ollama-cpu \
 && rm -rf /opt/ollama-cpu/cuda_* /opt/ollama-cpu/mlx_* /opt/ollama-cpu/vulkan
ARG MODELS="qwen3:4b qwen3.5:9b"
RUN (ollama serve >/tmp/serve.log 2>&1 &) \
 && for i in $(seq 1 30); do ollama list >/dev/null 2>&1 && break; sleep 1; done \
 && for m in $MODELS; do echo "downloading $m"; ollama pull "$m" >/dev/null || { cat /tmp/serve.log; exit 1; }; done \
 && ollama list

FROM ubuntu:24.04 AS models
COPY --from=dist /root/.ollama/models /root/.ollama/models
ENV OLLAMA_HOST=0.0.0.0:11434 \
    OLLAMA_MAX_LOADED_MODELS=1 \
    OLLAMA_NUM_PARALLEL=1 \
    OLLAMA_KEEP_ALIVE=15m
EXPOSE 11434
ENTRYPOINT ["/usr/bin/ollama"]
CMD ["serve"]

FROM models AS cpu
COPY --from=dist /usr/bin/ollama /usr/bin/ollama
COPY --from=dist /opt/ollama-cpu /usr/lib/ollama

FROM models AS gpu
COPY --from=dist /usr/bin/ollama /usr/bin/ollama
COPY --from=dist /usr/lib/ollama /usr/lib/ollama
ENV LD_LIBRARY_PATH=/usr/local/nvidia/lib:/usr/local/nvidia/lib64 \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility \
    NVIDIA_VISIBLE_DEVICES=all
