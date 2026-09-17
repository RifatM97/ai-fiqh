# ai-fiqh-ollama — the Azure-content-filter fallback (docs/deployment.md §4c).
#
# Bakes gemma4:12b into the image at build time (chosen over an Azure Files
# mount) so a scale-to-zero cold start never pays for a fresh model pull —
# only container start + Ollama server init. Build takes a while and produces
# a large image (~8 GB+); that trade-off is deliberate, see §4c.
#
#   docker build -f docker/ollama.Dockerfile -t ai-fiqh-ollama .

FROM ollama/ollama:latest

# Start the server, wait for it to be ready, pull the model so it lands in
# this layer, then stop the server — nothing about this image runs a
# background process past build time.
RUN (ollama serve &) && \
    for i in $(seq 1 15); do \
        ollama list >/dev/null 2>&1 && break; \
        sleep 2; \
    done && \
    ollama pull gemma4:12b && \
    (pkill ollama || true)

EXPOSE 11434

ENTRYPOINT ["ollama", "serve"]
