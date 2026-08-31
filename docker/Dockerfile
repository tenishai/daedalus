# daedalus runtime base image. Agent images layer on top:
#   FROM <ar>/daedalus-runtime:<tag>
#   COPY bundle/ /bundle
#
# techne is not on PyPI; it is supplied as a named build context:
#   docker build -f docker/Dockerfile --build-context techne=../techne -t daedalus-runtime .
#
# Build stage: wheels only. Both source trees (including techne's .git history,
# which .dockerignore cannot reach since it's a separate named build context)
# stay confined to this stage and never reach the final image layers.
#
# Transport contract: the a2a transport defaults to host 127.0.0.1 port 9200,
# which is unreachable from outside the container (kubelet probes and the
# Service both connect from outside the pod's loopback namespace). Bundles
# deployed on this image MUST declare in their config:
#   transports: [{ kind: a2a, host: 0.0.0.0, port: 8080 }]
# host 0.0.0.0 so probes/Service can reach it; port 8080 to match this
# platform's APP_PORT (see EXPOSE below).
FROM python:3.12-slim AS build

COPY --from=techne / /src/techne
COPY . /src/daedalus
# google-cloud-aiplatform: litellm's vertex_ai/* routes import the vertexai
# SDK at call time — the platform's default model provider, so baked in here.
# techne[vectors] (0.16.1 wave): resolve the vectors extra's dependency
# closure (sqlite-vec, numpy, fastembed/onnxruntime on py3.12) into wheels
# alongside everything else.
RUN pip wheel --wheel-dir /wheels "techne[vectors] @ file:///src/techne" "/src/daedalus[a2a,slack]" "google-cloud-aiplatform>=1.38"

FROM python:3.12-slim

RUN useradd --uid 10001 --create-home agent

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels "daedalus[a2a,slack]" "techne[vectors]" google-cloud-aiplatform && rm -rf /wheels
# Build proof: a stale cached techne wheel once shipped an image whose
# knowledge.py imported symbols its own database.py lacked -- every agent
# crashlooped at seed time. Fail the BUILD, not the fleet.
RUN python -c "import daedalus, techne; from techne.core.database import connect, like_escape; from techne.core import knowledge"

# Semantic recall: bake the embedding model into the image so a pod never
# reaches for the Hub (the seeder backfills with allow_download=False).
# HF_HOME pins the cache somewhere deterministic and user-independent; the
# model name mirrors techne's _DEFAULT_FASTEMBED_MODEL
# (techne/core/embeddings.py) -- if techne changes its default, this line
# must move with it or the bake warms the wrong cache.
ENV HF_HOME=/opt/hf-cache
RUN mkdir -p /opt/hf-cache \
    && python -c "from fastembed import TextEmbedding; TextEmbedding('sentence-transformers/all-MiniLM-L6-v2')" \
    && chmod -R a+rX /opt/hf-cache
# Build proof for the vectors path: the backend must resolve, and the baked
# model must load WITHOUT a download -- a cold cache here means the bake
# above warmed the wrong location, which would otherwise surface only as
# every pod silently serving FTS-only.
RUN python -c "from techne.core.embeddings import backend_available; assert backend_available(), 'no embedding backend'"
RUN python -c "from techne.core.embeddings import _get_embedding_model; list(_get_embedding_model(allow_download=False).embed(['build proof']))"

COPY docker/seeder.py /usr/local/bin/seeder.py
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENV TECHNE_HOME=/techne
RUN mkdir -p /techne && chown agent:agent /techne
VOLUME /techne

USER agent
EXPOSE 8080
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["/bundle"]
