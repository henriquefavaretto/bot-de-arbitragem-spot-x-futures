import { useEffect, useState } from 'react';
import Header from './components/Header';
import SummaryCards from './components/SummaryCards';
import ArbitrageTable from './components/ArbitrageTable';
import MultiExchangePanel from './components/MultiExchangePanel';
import BotPanel from './components/BotPanel';
import BotHistoryPanel from './components/BotHistoryPanel';
import BotLogsPanel from './components/BotLogsPanel';
import BalanceBar from './components/BalanceBar';
import { useArbitrageSocket } from './hooks/useArbitrageSocket';
import './App.css';

export default function App() {
  const { pairs, connectionStatus, getFlash, getSparkline, hiddenPairs } = useArbitrageSocket();
  const [theme, setTheme] = useState(() => localStorage.getItem('arb-theme') || 'dark');
  const [activeTab, setActiveTab] = useState('dashboard'); // 'dashboard' | 'multi' | 'bot' | 'history' | 'logs'

  useEffect(() => {
    document.documentElement.setAttribute('data-theme', theme);
    localStorage.setItem('arb-theme', theme);
  }, [theme]);

  const toggleTheme = () => setTheme((t) => (t === 'dark' ? 'light' : 'dark'));

  return (
    <div className="app-shell">
      <Header theme={theme} onToggleTheme={toggleTheme} />
      <BalanceBar />

      <nav className="app-tabs">
        <button
          className={`app-tab ${activeTab === 'dashboard' ? 'active' : ''}`}
          onClick={() => setActiveTab('dashboard')}
        >
          Dashboard
        </button>
        <button
          className={`app-tab ${activeTab === 'multi' ? 'active' : ''}`}
          onClick={() => setActiveTab('multi')}
        >
          Multi-Exchange
        </button>
        <button
          className={`app-tab ${activeTab === 'bot' ? 'active' : ''}`}
          onClick={() => setActiveTab('bot')}
        >
          Bot de Arbitragem
        </button>
        <button
          className={`app-tab ${activeTab === 'history' ? 'active' : ''}`}
          onClick={() => setActiveTab('history')}
        >
          Histórico
        </button>
        <button
          className={`app-tab ${activeTab === 'logs' ? 'active' : ''}`}
          onClick={() => setActiveTab('logs')}
        >
          Logs
        </button>
      </nav>

      <main className="app-main">
        <div style={{ display: activeTab === 'dashboard' ? 'flex' : 'none', flexDirection: 'column', gap: 18 }}>
          <SummaryCards pairs={pairs} connectionStatus={connectionStatus} />
          <ArbitrageTable pairs={pairs} getFlash={getFlash} getSparkline={getSparkline} hiddenPairs={hiddenPairs} />
        </div>
        {activeTab === 'multi' && <MultiExchangePanel />}

        <div style={{ display: activeTab === 'bot' ? 'block' : 'none' }}>
          <BotPanel dashboardPairs={pairs} />
        </div>
        <div style={{ display: activeTab === 'history' ? 'block' : 'none' }}>
          <BotHistoryPanel />
        </div>
        <div style={{ display: activeTab === 'logs' ? 'block' : 'none' }}>
          <BotLogsPanel />
        </div>
      </main>
    </div>
  );
}
