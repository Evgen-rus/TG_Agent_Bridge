import { afterEach } from 'vitest';
import { cleanup } from '@testing-library/react';
afterEach(cleanup);
HTMLDialogElement.prototype.showModal=function(){this.setAttribute('open','')};
HTMLDialogElement.prototype.close=function(){this.removeAttribute('open')};
