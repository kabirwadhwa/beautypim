FROM node:24-slim

WORKDIR /app

COPY package.json ./
RUN npm install

COPY . .

# In production Compose, we build, but for local workspace we bind mount
RUN npm run build

EXPOSE 3000

CMD ["npm", "start"]
