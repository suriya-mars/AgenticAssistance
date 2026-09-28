# Ollama with the models baked in: no download needed on the target machine, works offline.
# Models are staged from the build machine's local Ollama store by scripts/stage_models.py.
#
# Two variants share the same (large) models layer:
#   --target cpu  ~9 GB   Ollama CPU backends only (every x86-64 CPU generation, SSE4.2 .. AVX-512)
#   --target gpu  ~14 GB  adds CUDA 12/13 + Vulkan backends (NVIDIA GPUs; still falls back to CPU)
ARG OLLAMA_VERSION=0.34.4
FROM ollama/ollama:${OLLAMA_VERSION} AS dist
# CPU-only copy of the runtime: drop the GPU backends (~4.7 GB), keep licenses.
RUN cp -a /usr/lib/ollama /opt/ollama-cpu \
 && rm -rf /opt/ollama-cpu/cuda_* /opt/ollama-cpu/mlx_* /opt/ollama-cpu/vulkan

# Same OS as the official image. The models layer comes first so both variants share it.
FROM ubuntu:24.04 AS models
COPY build/models /root/.ollama/models
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
