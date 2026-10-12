import os

dropbot_path = r'C:\Users\bitress\Desktop\chopaeng\src\pages\DropBot.tsx'
with open(dropbot_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update dropFilter state
old_state = "const [dropFilter, setDropFilter] = useState<'all' | 'unlocked'>('all');"
new_state = "const [dropFilter, setDropFilter] = useState<'all' | 'sub' | 'free' | 'unlocked'>('all');"
if old_state in content:
    content = content.replace(old_state, new_state, 1)

# 2. Update island filtering logic
old_filters = """    // Drops & Villager Injections are strictly limited to VIP Sub Islands
    const subMemberIslands = useMemo(() => {
        return islands.filter(
            (i) => i.cat === 'member' || i.type?.toLowerCase().includes('sub')
        );
    }, [islands]);

    // Check if user has permission for a sub island
    const canAccessIsland = (island?: IslandData | null) => {
        if (!island) return false;
        if (island.cat !== 'member' && !island.type?.toLowerCase().includes('sub')) {
            return false; // Free islands cannot receive drops
        }
        if (!island.requiredRoles || island.requiredRoles.length === 0) return true;
        if (!user || !user.roles) return false;
        return island.requiredRoles.some((role) => user.roles.includes(role));
    };

    const unlockedSubIslands = useMemo(() => {
        return subMemberIslands.filter((i) => canAccessIsland(i));
    }, [subMemberIslands, user]);

    const availableDropIslands = useMemo(() => {
        if (dropFilter === 'unlocked') {
            return unlockedSubIslands;
        }
        return subMemberIslands;
    }, [subMemberIslands, unlockedSubIslands, dropFilter]);

    // Auto-select first accessible sub island
    useEffect(() => {
        if (!selectedDropIsland && subMemberIslands.length > 0) {
            const firstAccessible =
                subMemberIslands.find((i) => canAccessIsland(i)) ||
                subMemberIslands[0];
            setSelectedDropIsland(firstAccessible);
            if (firstAccessible?.dodoCode && firstAccessible.dodoCode !== 'SUB ONLY' && firstAccessible.dodoCode !== '.....') {
                setDropDodoCode(firstAccessible.dodoCode);
            }
        }
    }, [subMemberIslands, selectedDropIsland]);"""

new_filters = """    // Island category breakdown
    const subMemberIslands = useMemo(() => {
        return islands.filter(
            (i) => i.cat === 'member' || i.type?.toLowerCase().includes('sub') || i.type === 'VIP'
        );
    }, [islands]);

    const freeTreasureIslands = useMemo(() => {
        return islands.filter(
            (i) => i.cat === 'public' || i.type?.toLowerCase().includes('free')
        );
    }, [islands]);

    const orderBotIslands = useMemo(() => {
        return islands.filter(
            (i) => i.cat === 'order' || i.type?.toLowerCase().includes('order')
        );
    }, [islands]);

    // Check if user has permission for an island
    const canAccessIsland = (island?: IslandData | null) => {
        if (!island) return false;
        // Free and Order islands are public/open to everyone
        if (island.cat !== 'member' && !island.type?.toLowerCase().includes('sub') && island.type !== 'VIP') {
            return true;
        }
        if (user?.is_admin || user?.is_mod) return true;
        if (!island.requiredRoles || island.requiredRoles.length === 0) return true;
        if (!user || !user.roles) return false;
        return island.requiredRoles.some((role) => user.roles.includes(role));
    };

    const unlockedIslands = useMemo(() => {
        return islands.filter((i) => canAccessIsland(i));
    }, [islands, user]);

    const availableDropIslands = useMemo(() => {
        if (dropFilter === 'sub') return subMemberIslands;
        if (dropFilter === 'free') return freeTreasureIslands;
        if (dropFilter === 'unlocked') return unlockedIslands;
        return islands;
    }, [islands, subMemberIslands, freeTreasureIslands, unlockedIslands, dropFilter]);

    // Auto-select first accessible island
    useEffect(() => {
        if (!selectedDropIsland && islands.length > 0) {
            const firstAccessible =
                islands.find((i) => canAccessIsland(i)) ||
                islands[0];
            setSelectedDropIsland(firstAccessible);
            if (firstAccessible?.dodoCode && firstAccessible.dodoCode !== 'SUB ONLY' && firstAccessible.dodoCode !== '.....') {
                setDropDodoCode(firstAccessible.dodoCode);
            }
        }
    }, [islands, selectedDropIsland]);"""

if old_filters in content:
    content = content.replace(old_filters, new_filters, 1)

# 3. Remove sub-only restrictions
old_drop_check = """        if (selectedDropIsland.cat !== 'member' && !selectedDropIsland.type?.toLowerCase().includes('sub')) {
            setSubmissionNotice({
                type: 'danger',
                message: 'Item drops are only supported on VIP Sub Islands. Free islands cannot receive drops.',
            });
            return;
        }"""
if old_drop_check in content:
    content = content.replace(old_drop_check, '', 1)

old_inject_check = """        if (selectedDropIsland.cat !== 'member' && !selectedDropIsland.type?.toLowerCase().includes('sub')) {
            setSubmissionNotice({
                type: 'danger',
                message: 'Villager injections are only supported on VIP Sub Islands.',
            });
            return;
        }"""
if old_inject_check in content:
    content = content.replace(old_inject_check, '', 1)

old_batch_check = """        if (selectedDropIsland.cat !== 'member' && !selectedDropIsland.type?.toLowerCase().includes('sub')) {
            setSubmissionNotice({
                type: 'danger',
                message: 'Villager injections are only supported on VIP Sub Islands.',
            });
            return;
        }"""
if old_batch_check in content:
    content = content.replace(old_batch_check, '', 1)

# 4. Island Selector UI
old_ui_block = """                                    <div>
                                        <h3 className="h6 fw-bold mb-0 text-dark">Destination Island</h3>
                                        <span className="text-muted" style={{ fontSize: '0.75rem' }}>
                                            Select VIP Sub Island for item drops and villager injections
                                        </span>
                                    </div>
                                </div>

                                {/* Filter categories */}
                                <div className="d-flex gap-1 bg-light p-1 rounded-pill border">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('all');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'all'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        All Sub ({subMemberIslands.length})
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('unlocked');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'unlocked'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        My Pass ({unlockedSubIslands.length})
                                    </button>
                                </div>
                            </div>

                            {/* Island Dropdown */}
                            {islandsLoading ? (
                                <div className="text-center py-2 text-muted small">Loading sub islands…</div>
                            ) : (
                                <div className="mb-3">
                                    <select
                                        className="form-select rounded-4 border shadow-2xs fw-bold text-dark"
                                        style={{ fontSize: '0.88rem' }}
                                        value={selectedDropIsland?.id || ''}
                                        onChange={(e) => {
                                            const found = subMemberIslands.find((i) => i.id === e.target.value);
                                            setSelectedDropIsland(found || null);
                                            setAlreadyOnIsland(false);
                                            playSound();
                                        }}
                                    >
                                        <option value="">-- Choose Sub Island --</option>
                                        {availableDropIslands.map((isl) => (
                                            <option key={isl.id} value={isl.id}>
                                                ⭐ {isl.name} · {isl.type || 'Sub Island'}
                                            </option>
                                        ))}
                                    </select>
                                </div>
                            )}"""

new_ui_block = """                                    <div>
                                        <h3 className="h6 fw-bold mb-0 text-dark">Destination Island</h3>
                                        <span className="text-muted" style={{ fontSize: '0.75rem' }}>
                                            Select any island to reveal live Dodo boarding pass, drop items, or inject villagers
                                        </span>
                                    </div>
                                </div>

                                {/* Filter categories */}
                                <div className="d-flex gap-1 bg-light p-1 rounded-pill border flex-wrap">
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('all');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'all'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        All ({islands.length})
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('sub');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'sub'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        VIP Sub ({subMemberIslands.length})
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('free');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'free'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        Free ({freeTreasureIslands.length})
                                    </button>
                                    <button
                                        type="button"
                                        onClick={() => {
                                            playSound();
                                            setDropFilter('unlocked');
                                        }}
                                        className={`btn btn-xs rounded-pill fw-bold px-2.5 py-1 ${dropFilter === 'unlocked'
                                            ? 'btn-dark text-white'
                                            : 'text-muted border-0 bg-transparent'
                                            }`}
                                        style={{ fontSize: '0.72rem' }}
                                    >
                                        My Pass ({unlockedIslands.length})
                                    </button>
                                </div>
                            </div>

                            {/* Island Dropdown */}
                            {islandsLoading ? (
                                <div className="text-center py-2 text-muted small">Loading islands…</div>
                            ) : (
                                <div className="mb-3">
                                    <select
                                        className="form-select rounded-4 border shadow-2xs fw-bold text-dark"
                                        style={{ fontSize: '0.88rem' }}
                                        value={selectedDropIsland?.id || ''}
                                        onChange={(e) => {
                                            const found = islands.find((i) => i.id === e.target.value);
                                            setSelectedDropIsland(found || null);
                                            setAlreadyOnIsland(false);
                                            playSound();
                                        }}
                                    >
                                        <option value="">-- Choose Island ({availableDropIslands.length} Available) --</option>
                                        {dropFilter === 'all' ? (
                                            <>
                                                {subMemberIslands.length > 0 && (
                                                    <optgroup label="⭐ VIP Sub Member Islands">
                                                        {subMemberIslands.map((isl) => (
                                                            <option key={isl.id} value={isl.id}>
                                                                ⭐ {isl.name} · VIP Sub Island
                                                            </option>
                                                        ))}
                                                    </optgroup>
                                                )}
                                                {freeTreasureIslands.length > 0 && (
                                                    <optgroup label="🌴 Free Treasure Islands">
                                                        {freeTreasureIslands.map((isl) => (
                                                            <option key={isl.id} value={isl.id}>
                                                                🌴 {isl.name} · Free Treasure Island
                                                            </option>
                                                        ))}
                                                    </optgroup>
                                                )}
                                                {orderBotIslands.length > 0 && (
                                                    <optgroup label="🤖 Order Bot">
                                                        {orderBotIslands.map((isl) => (
                                                            <option key={isl.id} value={isl.id}>
                                                                🤖 {isl.name} · Order Bot
                                                            </option>
                                                        ))}
                                                    </optgroup>
                                                )}
                                            </>
                                        ) : (
                                            availableDropIslands.map((isl) => {
                                                const isSub = isl.cat === 'member' || isl.type === 'VIP' || isl.type?.toLowerCase().includes('sub');
                                                const icon = isSub ? '⭐' : (isl.cat === 'order' ? '🤖' : '🌴');
                                                return (
                                                    <option key={isl.id} value={isl.id}>
                                                        {icon} {isl.name} · {isl.type || (isSub ? 'VIP Sub' : 'Free')}
                                                    </option>
                                                );
                                            })
                                        )}
                                    </select>
                                </div>
                            )}"""

if old_ui_block in content:
    content = content.replace(old_ui_block, new_ui_block, 1)

# 5. Make Island Header Card dynamic
old_card_type = """                                                    <span className="text-muted tiny-text">
                                                        {selectedDropIsland.type || 'Sub Island'}
                                                    </span>"""
new_card_type = """                                                    <span className="text-muted tiny-text">
                                                        {selectedDropIsland.cat === 'member' || selectedDropIsland.type === 'VIP' || selectedDropIsland.type?.toLowerCase().includes('sub')
                                                            ? '⭐ VIP Sub Member Island'
                                                            : selectedDropIsland.cat === 'order'
                                                            ? '🤖 Order Bot Island'
                                                            : '🌴 Free Treasure Island'}
                                                    </span>"""
if old_card_type in content:
    content = content.replace(old_card_type, new_card_type, 1)

with open(dropbot_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("DropBot.tsx script completed successfully!")
