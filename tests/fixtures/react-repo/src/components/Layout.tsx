import { Link, Outlet } from "react-router-dom";

export default function Layout() {
  return (
    <div className="layout">
      <header>
        <Link to="/">SmartCart</Link>
        <p>Don't forget to check your orders. It's quick.</p>
      </header>
      <main>
        <Outlet />
      </main>
    </div>
  );
}
