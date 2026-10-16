import { createRoot } from 'react-dom/client';
import App from './App';
import Production from './Production';
import './styles.css';
createRoot(document.getElementById('root')!).render(/^\/(rick|sally)\//.test(location.pathname)?<Production/>:<App/>);
