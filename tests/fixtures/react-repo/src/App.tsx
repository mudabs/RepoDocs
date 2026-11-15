import React, { Suspense } from "react";
import { BrowserRouter, Route, Routes } from "react-router-dom";

import Layout from "./components/Layout";
import Home from "./pages/Home";
import { OrdersPage } from "./pages/OrdersPage";
import OrderDetail from "./pages/OrderDetail";

// The admin page is code-split: the import edge is recorded, the load time is not.
const Admin = React.lazy(() => import("./pages/Admin"));

const settingsPath = window.__SETTINGS_PATH__ ?? "/settings";

/* <Route path="/commented-out" /> inside a comment must be ignored. */
export default function App() {
  return (
    <BrowserRouter>
      <Suspense fallback={<p>Loading...</p>}>
        <Routes>
          <Route path="/" element={<Layout />}>
            <Route index element={<Home />} />
            <Route path="orders" element={<OrdersPage />} />
            <Route path="orders/:id" element={<OrderDetail />} />
            <Route path={settingsPath} element={<Admin />} />
          </Route>
        </Routes>
      </Suspense>
    </BrowserRouter>
  );
}
