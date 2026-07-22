const fs = require('fs');
const path = require('path');

const extensionRoot = path.resolve(__dirname, '..');
const workspaceRoot = path.resolve(extensionRoot, '..');
const sourcePackage = path.join(workspaceRoot, 'agentd', 'agent_hub');
const targetPackage = path.join(extensionRoot, 'daemon', 'agentd', 'agent_hub');
const sourceConfig = path.join(workspaceRoot, 'config.yaml');
const targetConfig = path.join(extensionRoot, 'daemon', 'config.yaml');

if (!fs.existsSync(path.join(sourcePackage, 'main.py')) || !fs.existsSync(sourceConfig)) {
    throw new Error('Agent Hub daemon sources or config.yaml are missing');
}

fs.mkdirSync(targetPackage, { recursive: true });
fs.cpSync(sourcePackage, targetPackage, {
    recursive: true,
    force: true,
    filter: (entry) => !entry.includes('__pycache__') && !entry.endsWith('.pyc'),
});
fs.copyFileSync(sourceConfig, targetConfig);
console.log(`Bundled daemon prepared at ${path.relative(extensionRoot, targetPackage)}`);
