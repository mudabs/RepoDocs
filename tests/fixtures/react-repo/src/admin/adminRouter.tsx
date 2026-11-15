import { createBrowserRouter } from "react-router-dom";

import Layout from "../components/Layout";
import Admin from "../pages/Admin";
import { OrdersPage } from "../pages/OrdersPage";

export const adminRouter = createBrowserRouter([
  {
    path: "/admin",
    element: <Layout />,
    children: [
      { index: true, element: <Admin /> },
      { path: "orders", element: <OrdersPage /> },
      { path: "users", lazy: () => import("../pages/Users") },
    ],
  },
]);
