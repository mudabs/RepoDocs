import React from "react";
import {api} from "./client";

export function App() {
    api.get("/orders");
    api.get(`/orders/${id}`);
    api.delete("/orders"); // method mismatch
    fetch("https://other.example.invalid/api/orders"); // conflicting host
    fetch("/api/orders"); // no host, so both backends remain possible
    fetch("https://alice:CredentialSecret@shop.example.invalid/api/orders?token=QuerySecret#FragmentSecret");
    return <main>SmartCart</main>;
}
