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
RUN pip wheel --wheel-dir /wheels /src/techne "/src/daedalus[a2a]"

FROM python:3.12-slim

RUN useradd --uid 10001 --create-home agent

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels "daedalus[a2a]" techne && rm -rf /wheels

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
