import { BrowserRouter, Routes, Route } from "react-router-dom";
import { EditorPage } from "./pages/EditorPage";
import { ViewerPage } from "./pages/ViewerPage";

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<EditorPage />} />
        <Route path="/edit/:id" element={<EditorPage />} />
        <Route path="/view/:id" element={<ViewerPage />} />
      </Routes>
    </BrowserRouter>
  );
}
