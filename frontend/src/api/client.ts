import axios from "axios";

// "localhost" always resolves to the *client's own* machine, which breaks when the
// frontend is opened from another device on the LAN. Default to whatever host the
// page itself was loaded from instead, so it keeps working over a LAN IP too.
const defaultBaseURL = `${window.location.protocol}//${window.location.hostname}:8000`;

export const apiClient = axios.create({
  baseURL: import.meta.env.VITE_API_BASE_URL ?? defaultBaseURL,
});
