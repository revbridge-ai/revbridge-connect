# RevBridge Connect — control panel, served by Caddy.
# Build context = the repository root. docker compose builds this as the image of the caddy service:
#   docker build -f docker/panel.Dockerfile -t revbridge/connect-caddy .
# Only the static files (no Caddy), to serve them somewhere else:
#   docker build -f docker/panel.Dockerfile --target files --output type=local,dest=./build/panel .

FROM node:22-alpine AS build

WORKDIR /panel

# Manifests first: caches the dependency install.
COPY panel/package.json panel/package-lock.json ./
RUN npm ci --no-audit --no-fund

# Source code only: a panel/node_modules from the host (binaries for another system) never gets in.
COPY panel/index.html panel/vite.config.ts panel/tsconfig.json panel/tailwind.config.js panel/postcss.config.js ./
COPY panel/public ./public
COPY panel/src ./src

# build = tsc --noEmit (fails on a type error) + vite build
RUN npm run build

FROM scratch AS files
COPY --from=build /panel/dist /srv/panel

# Final image: Caddy serving the panel at /srv/panel (the Caddyfile is mounted from config/caddy)
FROM caddy:2.8-alpine AS caddy
COPY --from=build /panel/dist /srv/panel
COPY config/caddy/ /etc/caddy/
