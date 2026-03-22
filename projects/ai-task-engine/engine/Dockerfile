FROM node:20-alpine AS builder

WORKDIR /app

# Install pnpm
RUN corepack enable && corepack prepare pnpm@latest --activate

COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --frozen-lockfile

COPY tsconfig.json ./
COPY src/ ./src/

RUN pnpm build

# Production image
FROM node:20-alpine

WORKDIR /app

RUN corepack enable && corepack prepare pnpm@latest --activate

COPY package.json pnpm-lock.yaml* ./
RUN pnpm install --prod --frozen-lockfile

COPY --from=builder /app/dist ./dist
COPY workflows/ ./workflows/

RUN mkdir -p data

EXPOSE 3200

ENV NODE_ENV=production
ENV API_PORT=3200
ENV DB_PATH=/app/data/task-engine.db
ENV EXECUTOR_MODE=mock

CMD ["node", "dist/index.js"]
