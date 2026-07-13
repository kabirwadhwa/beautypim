FROM node:24-slim

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# Declare build arguments injected by Railway during compilation
ARG NEXT_PUBLIC_API_URL
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL

# Build the Next.js application on high-memory build servers
RUN npm run build

EXPOSE 3000
CMD ["npm", "start"]
