import axios from "axios";
import { useParams } from "react-router-dom";

export default function OrderDetail() {
  const { id } = useParams();

  async function load() {
    const url = `/api/orders/${id}`;
    const response = await axios.get(`/api/orders/${id}`);
    const again = await fetch(url); // dynamic: the URL is a variable
    return [response.data, await again.json()];
  }

  async function submit(body: string) {
    await fetch("/api/orders", { method: "POST", body });
  }

  return (
    <article>
      <h1>Order {id}</h1>
      <button onClick={load}>Load</button>
      <button onClick={() => submit("{}")}>Submit</button>
    </article>
  );
}
