import { memo } from "react";

import type { Order } from "../types";
import { formatMoney } from "../utils/format";

type Props = { order: Order };

export const OrderCard = memo(function OrderCard({ order }: Props) {
  return (
    <article>
      <h2>Order {order.id}</h2>
      <span>{formatMoney(order.total)}</span>
    </article>
  );
});
