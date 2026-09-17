FROM ghcr.io/home-assistant/base:latest

RUN apk add --no-cache \
    python3 \
    py3-pip \
    py3-requests \
    py3-paho-mqtt

WORKDIR /app

COPY sync_workouts.py /app/sync_workouts.py
COPY toneget_service.py /app/toneget_service.py
COPY run.sh /run.sh

RUN chmod a+x /run.sh

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
    io.hass.version="${BUILD_VERSION}" \
    io.hass.type="app" \
    io.hass.arch="${BUILD_ARCH}"

CMD ["/run.sh"]