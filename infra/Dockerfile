# Playwright + Chromium on the AL2023 Lambda base image.
#
# NOTE: `playwright install --with-deps` uses apt and WILL FAIL here.
# AL2023 uses dnf, so the shared libraries are installed explicitly below.
FROM public.ecr.aws/lambda/python:3.12

RUN dnf install -y \
      nss nspr atk at-spi2-atk at-spi2-core cups-libs \
      libdrm libxkbcommon libXcomposite libXdamage libXext libXfixes libXrandr \
      mesa-libgbm alsa-lib pango cairo libxshmfence \
    && dnf clean all

ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
ENV AXE_SCRIPT_PATH=/var/task/vendor/axe.min.js

COPY requirements.txt ${LAMBDA_TASK_ROOT}/
RUN pip install --no-cache-dir -r ${LAMBDA_TASK_ROOT}/requirements.txt \
    && python -m playwright install chromium

COPY vendor/   ${LAMBDA_TASK_ROOT}/vendor/
COPY audit/    ${LAMBDA_TASK_ROOT}/audit/
COPY agent/    ${LAMBDA_TASK_ROOT}/agent/
COPY storage/  ${LAMBDA_TASK_ROOT}/storage/
COPY api/      ${LAMBDA_TASK_ROOT}/api/

CMD ["agent.handler.lambda_handler"]
