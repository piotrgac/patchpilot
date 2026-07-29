FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    systemd \
    systemd-sysv \
    openssh-server \
    sudo \
    curl \
    nginx \
    python3 \
    netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash deploy \
    && echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy \
    && mkdir -p /home/deploy/.ssh

RUN ssh-keygen -A

RUN systemctl enable ssh
RUN systemctl enable nginx

COPY tests/integration/fixtures/index.html /var/www/html/index.html

EXPOSE 22 80

STOPSIGNAL SIGRTMIN+3

CMD ["/sbin/init"]
