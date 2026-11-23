# Builds the TV messenger APK. Run android/messenger/build-in-docker.sh rather than this directly.
# The SDK's build tools only ship for x86-64, so this runs as amd64 (emulated on Apple silicon).
FROM --platform=linux/amd64 eclipse-temurin:17-jdk
ARG CMDLINE_TOOLS=commandlinetools-linux-11076708_latest.zip
RUN apt-get update \
 && apt-get install -y --no-install-recommends unzip zip wget \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /sdk/cmdline-tools \
 && wget -q https://dl.google.com/android/repository/$CMDLINE_TOOLS -O /tmp/tools.zip \
 && unzip -q /tmp/tools.zip -d /sdk/cmdline-tools \
 && mv /sdk/cmdline-tools/cmdline-tools /sdk/cmdline-tools/latest \
 && rm /tmp/tools.zip \
 && yes | /sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/sdk \
      "build-tools;34.0.0" "platforms;android-34" >/dev/null
