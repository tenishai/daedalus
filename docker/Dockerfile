# daedalus runtime base image. Agent images layer on top:
#   FROM <ar>/daedalus-runtime:<tag>
#   COPY bundle/ /bundle
#
# techne is not on PyPI; it is supplied as a named build context:
#   docker build -f docker/Dockerfile --build-context techne=../techne -t daedalus-runtime .
FROM python:3.12-slim

RUN useradd --uid 10001 --create-home agent

COPY --from=techne / /opt/techne
RUN pip install --no-cache-dir /opt/techne && rm -rf /opt/techne

COPY . /opt/daedalus
RUN pip install --no-cache-dir "/opt/daedalus[a2a]" && rm -rf /opt/daedalus

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
