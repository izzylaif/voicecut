import { createRoot } from 'react-dom/client';
import './styles.css';
import { App } from './App';
import { startTheme } from './theme';

startTheme();
const container = document.getElementById('root');
if (!container) throw new Error('VoiceCut editor: #root not found');
createRoot(container).render(<App />);
