import axios from "axios";

export const api = axios.create({ baseURL: "/api", timeout: 5000 });

export function Format(value: number): string {
  // PascalCase helper without JSX: not a component.
  return value.toFixed(2);
}
