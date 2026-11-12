import { BrowserRouter, Route, Routes } from 'react-router-dom';
import { Shell } from './components/Shell';
import { DashboardPage } from './pages/DashboardPage';
import { FuturesPage } from './pages/FuturesPage';
import { LivePage } from './pages/LivePage';
import { ModelsPage } from './pages/ModelsPage';
import { OptionsPage } from './pages/OptionsPage';
import { PredictionsPage } from './pages/PredictionsPage';
import { QualityPage } from './pages/DataQualityPage';
import { SettingsPage } from './pages/SettingsPage';
import { MarketMemoryPage } from './pages/MarketMemoryPage';
import { NewsPage } from './pages/NewsPage';
import { EvidenceJournalPage } from './pages/EvidenceJournalPage';
export default function App(){return <BrowserRouter><Routes><Route element={<Shell/>}><Route index element={<DashboardPage/>}/><Route path="live" element={<LivePage/>}/><Route path="predictions" element={<PredictionsPage/>}/><Route path="options" element={<OptionsPage/>}/><Route path="futures" element={<FuturesPage/>}/><Route path="models" element={<ModelsPage/>}/><Route path="history" element={<PredictionsPage history/>}/><Route path="memory" element={<MarketMemoryPage/>}/><Route path="news" element={<NewsPage/>}/><Route path="evidence" element={<EvidenceJournalPage/>}/><Route path="data-quality" element={<QualityPage/>}/><Route path="settings" element={<SettingsPage/>}/></Route></Routes></BrowserRouter>}
