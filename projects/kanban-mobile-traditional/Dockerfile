FROM python:3.12-alpine AS build
WORKDIR /app
COPY --chmod=644 src ./src
COPY --chmod=644 scripts/build.py ./scripts/build.py
COPY --chmod=644 fixtures/example-snapshot.json ./fixtures/example-snapshot.json
RUN python3 scripts/build.py --snapshot fixtures/example-snapshot.json --output /out/index.html

FROM nginx:1.27-alpine
COPY --from=build --chmod=644 /out/index.html /usr/share/nginx/html/index.html
RUN find /usr/share/nginx/html -type d -exec chmod 755 {} +
