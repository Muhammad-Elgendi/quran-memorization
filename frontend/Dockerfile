# syntax=docker/dockerfile:1

FROM node:22-alpine AS build

WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm install
COPY . .

ARG VITE_API_BASE_URL=
ARG VITE_AUDIO_DENOISE=off
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_AUDIO_DENOISE=$VITE_AUDIO_DENOISE
RUN npm run build

FROM nginx:1.27-alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
