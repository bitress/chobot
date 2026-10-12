with open(r'c:\Users\bitress\Desktop\chopaeng\src\pages\DropBot.tsx', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Imports
target_imp = "} from '../utils/dropBotApi';"
repl_imp = """} from '../utils/dropBotApi';
import { useSavedCharacters } from '../hooks/useSavedCharacters';
import SubRulesModal from '../components/sub/SubRulesModal';
import { isSubRulesAccepted } from '../utils/subRulesStorage';"""
assert target_imp in text, 'target_imp not found'
text = text.replace(target_imp, repl_imp, 1)

# 2. Hooks and state
target_state = 'const [dropDodoError, setDropDodoError] = useState<string | null>(null);'
repl_state = '''const [dropDodoError, setDropDodoError] = useState<string | null>(null);
    const { hasSavedCharacters } = useSavedCharacters();
    const [showSubRulesModal, setShowSubRulesModal] = useState(false);'''
assert target_state in text, 'target_state not found'
text = text.replace(target_state, repl_state, 1)

# 3. Guard in handleGetDodo
target_guard = '''    const handleGetDodo = async (island: IslandData | null) => {
        if (!island) return;
        setDropDodoLoading(true);
        setDropDodoError(null);
        playSound();

        try {'''

repl_guard = '''    const handleGetDodo = async (island: IslandData | null) => {
        if (!island) return;
        setDropDodoError(null);

        if (!hasSavedCharacters) {
            setDropDodoError(
                "You must save your In-Game Character (IGN & Island Name) first before revealing Dodo codes. Please add your character in Profile Settings."
            );
            return;
        }

        const isSubMember = Boolean(user?.is_subscriber || user?.sub_tier || user?.is_mod || user?.is_admin);
        if (isSubMember && !isSubRulesAccepted(user?.user_id)) {
            setShowSubRulesModal(true);
            return;
        }

        setDropDodoLoading(true);
        playSound();

        try {'''
assert target_guard in text, 'target_guard not found'
text = text.replace(target_guard, repl_guard, 1)

# 4. JSX modal near bottom
target_jsx = '{/* VISIBLE UNOFFICIAL FAN-SITE DISCLAIMER */}'
repl_jsx = '''<SubRulesModal
                isOpen={showSubRulesModal}
                onClose={() => setShowSubRulesModal(false)}
                onAccept={() => {
                    setShowSubRulesModal(false);
                    if (selectedDropIsland) handleGetDodo(selectedDropIsland);
                }}
                userId={user?.user_id}
            />

            {/* VISIBLE UNOFFICIAL FAN-SITE DISCLAIMER */}'''
assert target_jsx in text, 'target_jsx not found'
text = text.replace(target_jsx, repl_jsx, 1)

with open(r'c:\Users\bitress\Desktop\chopaeng\src\pages\DropBot.tsx', 'w', encoding='utf-8') as f:
    f.write(text)

print('DropBot.tsx updated successfully!')
