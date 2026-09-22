# syntax=docker/dockerfile:1

# Base images come from Microsoft Container Registry. Docker Hub is used only
# where MCR has no equivalent (see benchmark/README.md for the image policy).
#
# Dependency resolution happens here, at build time, and never inside a scored
# trial: the runtime stage is a distroless image with no shell and no package
# manager, so trial containers fetch nothing from any package registry. See
# "Package supply chain compliance" in docs/specs/copilot-radius-experiment-plan.md.
#
# Go modules are fetched from the public proxy. This is a declared exception:
# CFS does not yet carry Go (npm, NuGet and PyPI only), and the internal
# Athens-based proxy is reachable only through 1ES Pipeline Templates, which
# this repository does not use. Revisit if Go onboards to CFS or this repo
# moves into a 1ES pipeline. go.sum gives cryptographic verification of every
# module in the meantime.
FROM mcr.microsoft.com/oss/go/microsoft/golang:1.23-bookworm AS build

# Pinned exactly rather than left at "auto", which would silently download a
# different toolchain if go.mod asked for one: a network fetch during the
# hermetic stage and a reproducibility break at the same time.
ENV GOTOOLCHAIN=go1.23.12 \
    GOFLAGS=-mod=readonly

WORKDIR /src

# Module cache is primed in its own layer, keyed only on go.mod/go.sum, so it
# stays valid across source edits.
COPY go.mod go.sum ./
RUN go mod download

# Everything from here builds with the proxy switched off. If the cache primed
# above is incomplete, the build fails closed instead of quietly reaching out to
# the network. Verified by a negative test: removing an entry from the primed
# cache makes this step fail rather than re-fetch.
COPY . .
RUN GOPROXY=off CGO_ENABLED=0 GOOS=linux \
    go build -trimpath -ldflags="-s -w" -o /out/catalog-api ./cmd/catalog-api

FROM mcr.microsoft.com/azurelinux/distroless/base:3.0
COPY --from=build /out/catalog-api /catalog-api
EXPOSE 8080
USER 65532:65532
ENTRYPOINT ["/catalog-api"]
