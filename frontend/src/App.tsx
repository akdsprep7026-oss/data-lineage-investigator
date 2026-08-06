import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { Layout } from "./components/Layout";
import { DetailPage } from "./pages/DetailPage";
import { HistoryPage } from "./pages/HistoryPage";
import { SubmitPage } from "./pages/SubmitPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<SubmitPage />} />
          <Route path="history" element={<HistoryPage />} />
          <Route path="investigations/:id" element={<DetailPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
    </BrowserRouter>
  );
}
