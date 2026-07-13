FROM node:24-slim

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# We compile at runtime to ensure Next.js has access to environment variables injected on boot
EXPOSE 3000
CMD ["sh", "-c", "npm run build && npm start"]
