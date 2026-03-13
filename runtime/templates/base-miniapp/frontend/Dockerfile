FROM node:22-alpine AS build

WORKDIR /app

ARG VITE_AUTH_ENDPOINT=/api/auth/telegram
ARG VITE_API_BASE_URL=
ARG VITE_DEFAULT_ROLE=client

ENV VITE_AUTH_ENDPOINT=${VITE_AUTH_ENDPOINT}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}
ENV VITE_DEFAULT_ROLE=${VITE_DEFAULT_ROLE}

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

FROM nginx:1.27-alpine

COPY --from=build /app/dist /usr/share/nginx/html
