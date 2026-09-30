.PHONY: install test lint check

install:
	pip install -e .[dev]

test:
	pytest -q

lint:
	ruff check src tests docker conftest.py

check: lint test

# --- runtime image (docker/) -------------------------------------------------
IMAGE ?= daedalus-runtime
TAG ?= $(shell git rev-parse --short HEAD)
AR_IMAGE ?=  # e.g. <location>-docker.pkg.dev/<project>/<repo>/daedalus-runtime

# Sibling checkouts by default (the compose layout). Set INDEX_URL (and
# AR_TOKEN for a private Artifact Registry index) to build without them.
INDEX_URL ?=
AR_TOKEN ?=
DEPS := $(if $(INDEX_URL),--build-arg EXTRA_INDEX_URL=$(INDEX_URL) --build-arg AR_TOKEN=$(AR_TOKEN),--build-context techne=../techne --build-context liminal=../liminal)

image:
	docker build --platform linux/amd64 -f docker/Dockerfile $(DEPS) -t $(IMAGE):$(TAG) -t $(IMAGE):latest .

image-smoke: image
	docker run --rm --entrypoint daedalus $(IMAGE):$(TAG) version
	docker run --rm --entrypoint python $(IMAGE):$(TAG) -c "import techne; print('techne ok')"

image-push: image
	test -n "$(AR_IMAGE)" || (echo "set AR_IMAGE=<ar-path>/daedalus-runtime" && exit 1)
	docker tag $(IMAGE):$(TAG) $(AR_IMAGE):$(TAG)
	docker push $(AR_IMAGE):$(TAG)
