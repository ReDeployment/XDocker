FROM --platform=$BUILDPLATFORM alpine:3.22 AS download
ARG TARGETARCH
WORKDIR /tmp
RUN apk add --no-cache ca-certificates curl
COPY checksums.txt ./checksums.txt
RUN case "$TARGETARCH" in amd64|arm64) ;; *) exit 1 ;; esac && \
    curl -fSL --retry 3 -o "frp_0.52.3_linux_${TARGETARCH}.tar.gz" \
      "https://github.com/fatedier/frp/releases/download/v0.52.3/frp_0.52.3_linux_${TARGETARCH}.tar.gz" && \
    grep " frp_0.52.3_linux_${TARGETARCH}.tar.gz$" checksums.txt | sha256sum -c - && \
    tar -xzf "frp_0.52.3_linux_${TARGETARCH}.tar.gz"

FROM alpine:3.22
ARG TARGETARCH
RUN apk add --no-cache ca-certificates
COPY --from=download /tmp/frp_0.52.3_linux_${TARGETARCH}/frps /usr/local/bin/frps
COPY --from=download /tmp/frp_0.52.3_linux_${TARGETARCH}/frpc /usr/local/bin/frpc
LABEL org.opencontainers.image.source="https://github.com/ReDeployment/XDocker"
LABEL org.opencontainers.image.description="FRP 0.52.3 shared tunnel service"
EXPOSE 7000 8080
ENTRYPOINT ["frps"]
CMD ["-c", "/etc/frp/frps.ini"]
