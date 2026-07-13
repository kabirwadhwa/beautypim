export const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
export const BACKEND_URL = API_URL.endsWith("/api") ? API_URL.substring(0, API_URL.length - 4) : API_URL;
