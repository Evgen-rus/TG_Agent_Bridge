import ts from 'typescript-eslint';
import hooks from 'eslint-plugin-react-hooks';
export default ts.config({ ignores: ['dist/**', 'node_modules/**'] }, ...ts.configs.recommended, {files: ['src/**/*.{ts,tsx}'], plugins: {'react-hooks': hooks}, rules: hooks.configs.recommended.rules});
