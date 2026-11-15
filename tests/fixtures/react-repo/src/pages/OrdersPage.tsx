import { useEffect, useState } from "react";

import { api } from "../api/client";
import { OrderCard } from "../components/OrderCard";
import type { Order } from "../types";

const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export function OrdersPage() {
  const [orders, setOrders] = useState<Order[]>([]);

  useEffect(() => {
    api.get("/orders").then((response) => setOrders(response.data));
  }, []);

  async function reload(id: string) {
    const response = await fetch(`${API_BASE}/orders/${id}`);
    return response.json();
  }

  return (
    <section>
      {orders.map((order) => (
        <OrderCard key={order.id} order={order} />
      ))}
      <button onClick={() => reload("1")}>Reload</button>
    </section>
  );
}
