const getApiUrl = () => {
  const url = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000/api";
  // Strip trailing slashes first
  const cleanUrl = url.endsWith("/") ? url.substring(0, url.length - 1) : url;
  // If it doesn't end with /api, append it automatically
  return cleanUrl.endsWith("/api") ? cleanUrl : `${cleanUrl}/api`;
};

export const API_URL = getApiUrl();
export const BACKEND_URL = API_URL.substring(0, API_URL.length - 4);
