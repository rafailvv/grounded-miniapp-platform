FROM node:20-alpine AS build

WORKDIR /app

COPY platform/frontend/package*.json ./
RUN npm ci

COPY platform/frontend ./

ARG VITE_PLATFORM_NAME="Upmini AI Studio"
ENV VITE_PLATFORM_NAME=${VITE_PLATFORM_NAME}

RUN npm run build

FROM nginx:1.27-alpine

COPY platform/frontend/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
