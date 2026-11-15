import axios from "axios";

// Client configuration is evidence, not a backend ownership assertion.
export const api = axios.create({baseURL: "https://shop.example.invalid/api"});
