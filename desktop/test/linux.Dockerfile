FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update -qq && apt-get install -y --no-install-recommends ca-certificates curl python3 git bubblewrap xvfb xauth libgtk-3-0 libnss3 libasound2t64 libgbm1 libnotify4 libxss1 libxtst6 libfuse2 xz-utils rpm binutils && rm -rf /var/lib/apt/lists/*
# Node distribution is pinned and checked before unpacking.
RUN curl -fsSLO https://nodejs.org/dist/v22.14.0/node-v22.14.0-linux-x64.tar.xz && echo '69b09dba5c8dcb05c4e4273a4340db1005abeafe3927efda2bc5b249e80437ec  node-v22.14.0-linux-x64.tar.xz' | sha256sum -c - && tar -xJf node-v22.14.0-linux-x64.tar.xz -C /usr/local --strip-components=1 && rm node-v22.14.0-linux-x64.tar.xz
COPY . /opt/piper
WORKDIR /opt/piper/desktop
RUN npm ci && npm run prepare:resources && node build/python.mjs linux x64
# ubuntu:24.04 ships the unprivileged user ubuntu (uid 1000)
RUN chown -R ubuntu:ubuntu /opt/piper
USER ubuntu
ENV APPIMAGE_EXTRACT_AND_RUN=1
CMD ["sh", "-c", "npm test && xvfb-run -a npm run e2e && npm run dist -- --linux AppImage deb --x64 --publish never"]
