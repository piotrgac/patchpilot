FROM rockylinux:9

RUN dnf install -y \
    systemd \
    openssh-server \
    sudo \
    curl \
    nginx \
    python3 \
    nmap-ncat \
    && dnf clean all

RUN useradd -m -s /bin/bash deploy \
    && echo "deploy ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/deploy \
    && mkdir -p /home/deploy/.ssh

RUN ssh-keygen -A

RUN systemctl enable sshd
RUN systemctl enable nginx

COPY tests/integration/fixtures/index.html /usr/share/nginx/html/index.html
RUN sed -i 's/80/8080/' /etc/nginx/nginx.conf

EXPOSE 22 8080

CMD ["/sbin/init"]
