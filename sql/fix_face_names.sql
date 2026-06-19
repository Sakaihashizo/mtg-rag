\set ON_ERROR_STOP on
BEGIN;
CREATE TEMP TABLE _fix(en text, ja text) ON COMMIT DROP;
INSERT INTO _fix VALUES
('Defacing Duskmage // Vandal''s Edit','落書きの薄暮魔道士 // 蛮人による編集'),
('Eccentric Pestfinder // Turn Stones','変わり者の害獣探し // 石の裏返し'),
('Inspired Skypainter // Maestro''s Gift','見事なる天描師 // 巨匠の贈り物'),
('Lorehold Archivist // Restore Relic','ロアホールドの文書管理人 // 秘宝の修復'),
('Naktamun Lorespinner // Wheel of Fortune','ナクタムンの伝承紡ぎ // 運命の輪'),
('Striding Shotcaller // Run the Play','一足飛びの司令塔 // 作戦通り'),
('Abigale, Poet Laureate // Heroic Stanza','桂冠詩人、アビゲール // 英雄的一節'),
('Adventurous Eater // Have a Bite','ゲテモノ喰らい // ひとかじり'),
('Campus Composer // Aqueous Aria','構内の作曲家 // 水のアリア'),
('Elite Interceptor // Rejoinder','精鋭の迎撃手 // 再答弁'),
('Goblin Glasswright // Craft with Pride','ゴブリンのガラス職人 // 誇りを込めた製作'),
('Honorbound Page // Forum''s Favor','名誉にかける小姓 // 討論所のはからい'),
('Jadzi, Steward of Fate // Oracle''s Gift','運命の執事、ジャズィ // 神託者の贈り物'),
('Kirol, History Buff // Pack a Punch','歴史通、キーロル // 強拳の持ち主'),
('Landscape Painter // Vibrant Idea','風景画家 // 鮮烈なアイデア'),
('Leech Collector // Bloodletting','ヒルの収集家 // 流血'),
('Lluwen, Exchange Student // Pest Friend','交換留学生、ルーウェン // 害獣の親友'),
('Maelstrom Artisan // Rocket Volley','大渦の職工 // ロケット斉射'),
('Pigment Wrangler // Striking Palette','絵の具の世話人 // 目を見張るパレット'),
('Quill-Blade Laureate // Twofold Intent','羽ペン剣の桂冠詩人 // 二重の意図'),
('Sanar, Unfinished Genius // Wild Idea','荒削りな天才、サナール // 突飛なアイデア'),
('Scathing Shadelock // Venomous Words','容赦なき妨術士 // 毒ある言葉'),
('Skycoach Conductor // All Aboard','飛空バスの車掌 // 出発進行'),
('Spiritcall Enthusiast // Scrollboost','精霊喚びの愛好者 // 巻物増幅'),
('Strife Scholar // Awaken the Ages','相争う学生 // 暦年の目覚め'),
('Tam, Observant Sequencer // Deep Sight','観察眼ある配列者、タム // 深遠なる視野'),
('Vastlands Scavenger // Bind to Life','大界の回収者 // 生命への束縛');

-- card_name が DB と一致する件数（27 期待）
SELECT count(*) AS matched FROM _fix f JOIN mtg_cards_v2 c ON c.card_name = f.en;
-- 一致しなかった行（タイポ検出・0 期待）
SELECT f.en AS unmatched FROM _fix f WHERE NOT EXISTS (SELECT 1 FROM mtg_cards_v2 c WHERE c.card_name = f.en);

UPDATE mtg_cards_v2 c SET japanese_name = f.ja FROM _fix f WHERE c.card_name = f.en;
COMMIT;
