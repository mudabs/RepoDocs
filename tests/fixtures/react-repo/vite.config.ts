import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Build configuration is not application code and must not become a module node.
export default defineConfig({ plugins: [react()] });
