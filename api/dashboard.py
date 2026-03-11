"""
ChoBot Web Dashboard
Mod-only web interface for island management, XLog reports, and analytics.
Access is protected by a secret key (DASHBOARD_SECRET env var).
"""

import json
import os
import sqlite3
import logging
import mimetypes
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.client import Config as BotocoreConfig
from botocore.exceptions import ClientError, NoCredentialsError

from flask import (
    Blueprint, render_template, request, redirect,
    url_for, session, flash, jsonify,
)

from utils.config import Config

logger = logging.getLogger("Dashboard")

# ---------------------------------------------------------------------------
# Blueprint setup
# ---------------------------------------------------------------------------
dashboard = Blueprint(
    "dashboard",
    __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/static",
)

# Absolute path to the shared SQLite database
_DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "chobot.db",
)

ALLOWED_CATEGORIES = ("public", "member")
ALLOWED_THEMES     = ("pink", "teal", "purple", "gold")
ALLOWED_STATUSES   = ("ONLINE", "SUB ONLY", "REFRESHING", "OFFLINE")

# Max map upload size: 5 MB
MAX_MAP_SIZE      = 5 * 1024 * 1024
ALLOWED_MAP_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


# ---------------------------------------------------------------------------
# All 47 island seeds  (from the TypeScript ISLANDS_DATA constant)
# ---------------------------------------------------------------------------
_SAMPLE_ISLANDS = [
    {"id":"adhika","name":"ADHIKA","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Pink Furniture Items","Pink Decoration Items","Pink Wall-Mounted Items","Pink Ceiling Items","Villagers"],"visitors":2,"cat":"member","theme":"gold","description":"Subscriber-only treasure island dedicated exclusively to pink items (furniture, decorations, wall-mounted, ceiling) and villagers.","dodo_code":None},
    {"id":"alapaap","name":"ALAPAAP","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":2,"cat":"member","theme":"gold","description":"Subscriber-only treasure island packed with full 2.0 content, DIYs, vehicles, gyroids, and villagers in boxes.","dodo_code":None},
    {"id":"aruga","name":"ARUGA","status":"SUB ONLY","type":"Patreon Exclusive","seasonal":"Year-Round","items":["Cherry Blossom Items","Young Spring Bamboo Items","Summer Items","Tree's Bounty Items","Maple Leaf Items","Mush Items","Ice Furniture","Christmas (Festive) Items","January Seasonal Items","February Seasonal Items","March Seasonal Items","April Seasonal Items","May Seasonal Items","June Seasonal Items","July Seasonal Items","August Items","September Items","October & November Seasonal Items","December Seasonal Items","Bunny Day Items","Festivale Items","Materials","Golden Tools","Nook Miles Ticket","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Patreon-exclusive seasonal island featuring complete event items, materials, golden tools, and rotating monthly sets.","dodo_code":None},
    {"id":"bahaghari","name":"BAHAGHARI","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":4,"cat":"member","theme":"gold","description":"Subscriber-only treasure island offering a balanced mix of 2.0 items, DIYs, vehicles, food, and villagers.","dodo_code":None},
    {"id":"bituin","name":"BITUIN","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Bathroom Appliances","Electronics","Tables","Toys","Home Decor","Home Appliances","Beds","Outdoor Decor","Vanity Mirror","Foods","Camping Items","Beach Items","Garden Decor","Plants","Lamps & Lights","Nook's Sports Items","Chairs & Sofas","Garden Items","Musical Instruments","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-only furniture island focused on household items, decor, electronics, plants, and lifestyle essentials.","dodo_code":None},
    {"id":"bonita","name":"BONITA","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["All Tops","All Bottoms","All Dress-Up","All Headwear","All Shoes","All Bags","All Accessories","Umbrellas","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-only fashion island with complete clothing sets, accessories, bags, shoes, and umbrellas.","dodo_code":None},
    {"id":"dalisay","name":"DALISAY","status":"SUB ONLY","type":"Patreon Exclusive","seasonal":"Year-Round","items":["DIY Equipment","DIY Housewares","DIY Miscellaneous","DIY Rug Wallpaper Flooring","DIY Tools Wall Mounted Fence and Others","DIY Savory & Sweets","Fish","Sea Creatures","Bugs","Fruits","Full Grown Trees","Flower Seeds","Flowers","Gyroids","Fence","Wallpaper & Flooring","Wrapping Paper","NMT Bells Royal Crown","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Patreon-exclusive resource island featuring DIYs, critters, plants, materials, currency items, and villagers.","dodo_code":None},
    {"id":"galak","name":"GALAK","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Cherry Blossom Items","Young Spring Bamboo Items","Summer Items","Tree's Bounty Items","Maple Leaf Items","Mush Items","Ice Furniture","Christmas (Festive) Items","January Seasonal Items","February Seasonal Items","March Seasonal Items","April Seasonal Items","May Seasonal Items","June Seasonal Items","July Seasonal Items","August Items","September Items","October & November Seasonal Items","December Seasonal Items","Bunny Day Items","Festivale Items","Materials","Golden Tools","Nook Miles Ticket","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-only seasonal island stocked with event furniture, materials, golden tools, and rare collectibles.","dodo_code":None},
    {"id":"giliw","name":"GILIW","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Pink Furniture Items","Pink Decoration Items","Pink Wall-Mounted Items","Pink Ceiling Items","Villagers"],"visitors":2,"cat":"member","theme":"gold","description":"Subscriber-only treasure island dedicated exclusively to pink items (furniture, decorations, wall-mounted, ceiling) and villagers.","dodo_code":None},
    {"id":"hiraya","name":"HIRAYA","status":"SUB ONLY","type":"Patreon Exclusive","seasonal":"Year-Round","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Ratan Set","Robot Heroes","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Butterfly Models","Wedding Items","Celeste Items","Bunny Day Items","K.K. Slider Songs","Library Items","Mats","Kitchen Items","NMT Customization Kits","Fish Baits","Pitfall Seeds","Zodiac Items","Seasonal Items","Gulliver Items","Trash Items","Hello Kitty","Gullivar Items","Mermaid Items","Moms Items","Flowers","Bug Off Fishing Tourney Gift Wrapper","Golden Tools","Simple Panels","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Patreon-exclusive curated island featuring premium furniture sets, Sanrio items, Celeste items, and rare decor.","dodo_code":None},
    {"id":"lakan","name":"LAKAN","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["Bathroom Appliances","Home Appliances","Electronics","Beds","Tables","Chairs & Sofas","Toys","Vanity Mirror","Home Decor","Outdoor Decor","Plants","Foods","Lamps & Lights","Garden Items","Garden Decor","Camping Items","Beach Items","Nooks Sports Items","Musical Instruments","Turnips","Villagers in Boxes"],"visitors":1,"cat":"member","theme":"gold","description":"Subscriber-only furniture island offering a wide selection of indoor, outdoor, and themed home furnishings.","dodo_code":None},
    {"id":"likha","name":"LIKHA","status":"SUB ONLY","type":"Treasure Island","seasonal":"Year-Round","items":["DIY Equipment","DIY Housewares","Fish","Sea Creatures","Bugs","Full Grown Trees","Flowers","Flower Seeds","Bush","DIY Miscellaneous","DIY Rug","Wallpaper","Flooring","DIY Tools","Wall Mounted Items","Fence","DIY Savory & Sweets","Gyroids","Wallpaper & Flooring","Wrapping Paper","NMT","Bells","Royal Crown","Turnips","Villagers in Boxes"],"visitors":1,"cat":"member","theme":"gold","description":"Subscriber-only materials and DIY island filled with crafting resources, critters, plants, and essential tools.","dodo_code":None},
    {"id":"marahuyo","name":"MARAHUYO","status":"SUB ONLY","type":"Patreon Exclusive","seasonal":"Year-Round","items":["Critters","Shrubs / Bush","Flowers","Complete DIY 1","Complete DIY 2","Mats","K.K. Slider","Kicks Items","Musical Instruments","Fences","Fossils","Bookshelf","Manhole Cover","Floor Lights","Special Character Posters","Paintings and Statues","Sahara Items","Complete Wrapping Paper","All House Plant Items","Villager Photos","Nook Miles Ticket","Updated Items","Royal Crown","Trash","Bugs and Fish Models","Simple Panels","Seasonal Nook Items","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Patreon-exclusive island focused on rare items, complete DIYs, art, villager photos, and high-value collectibles.","dodo_code":None},
    {"id":"tagumpay","name":"TAGUMPAY","status":"SUB ONLY","type":"Patreon Exclusive","seasonal":"Year-Round","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Rattan Set","Robot Heroes","Kitchen Items","Mats","Simple Panels","Library Items","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Wedding Items","Bunny Day Items","Seasonal Items","Zodiac Items","Bug-Off","Fishing Tourney","Gift Wrapper","Flowers","Butterfly Models","Celeste Items","Gulliver Items","Gullivar Items","Mom's Items","Mermaid Items","Sanrio Items","Hello Kitty","Golden Tools","Turnips","NMT Customization Kits","Fish Baits","Pitfall Seeds","Trash Items","K.K. Slider Songs","Updated Items","Villagers in Boxes"],"visitors":1,"cat":"member","theme":"gold","description":"Patreon-exclusive island showcasing carefully curated premium and exclusive item sets.","dodo_code":None},
    {"id":"kilig","name":"KILIG","status":"ONLINE","type":"1.0 Treasure Island","seasonal":"Year-Round","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Ratan Set","Robot Heroes","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Butterfly Models","Wedding Items","Celeste Items","Bunny Day Items","K.K. Slider Songs","Library Items","Mats","Kitchen Items","Zodiac Items","Seasonal Items","Moms Items","Flowers","Bug Off","Fishing Tourney","Gift Wrapper","Sanrio Items","Updated Items","Golden Tools","Simple Panels","Turnips","Villagers in Boxes","NMT","Customization Kits","Fish Baits","Pitfall Seeds","Gulliver Items","Trash Items","Hello Kitty","Gullivar Items","Mermaid Items"],"visitors":4,"cat":"public","theme":"teal","description":"Classic 1.0 treasure island featuring complete furniture sets, rare items, DIYs, and villagers in boxes.","dodo_code":"6FVD0"},
    {"id":"maharlika","name":"MAHARLIKA","status":"ONLINE","type":"Furniture Island","seasonal":"Year-Round","items":["Bathroom Appliances","Home Appliances","Electronics","Beds","Tables","Chairs & Sofas","Toys","Vanity Mirror","Home Decor","Outdoor Decor","Plants","Foods","Lamps & Lights","Garden Items","Garden Decor","Camping Items","Beach Items","Nooks Sports Items","Musical Instruments","Turnips","Villagers in Boxes"],"visitors":4,"cat":"public","theme":"purple","description":"Furniture-focused island offering elegant housewares, appliances, decor, and complete home essentials.","dodo_code":"2V23R"},
    {"id":"harana","name":"HARANA","status":"ONLINE","type":"Critters & DIY","seasonal":"Year-Round","items":["Critters","Shrubs / Bush","Flowers","Complete DIY 1","Complete DIY 2","Mats","K.K. Slider","Kicks Items","Musical Instruments","Fences","Fossils","Bookshelf Manhole Cover Floor Lights","Special Character Posters","Paintings and Statues","Complete Wrapping Paper","All House Plant Items","Saharah Items","Fruits","Villager Photos","Nook Miles Ticket","Updated Items","Trash","Bugs and Fish Models","Seasonal Nook Items","Turnips","Villagers in Boxes"],"visitors":4,"cat":"public","theme":"teal","description":"Curated island for DIYs, art, critter models, posters, and must-have progression items.","dodo_code":"J8QLW"},
    {"id":"kakanggata","name":"KAKANGGATA","status":"ONLINE","type":"1.0 Treasure Island","seasonal":"Summer","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Ratan Set","Robot Heroes","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Butterfly Models","Wedding Items","Celeste Items","Bunny Day Items","K.K. Slider Songs","Library Items","Mats","Kitchen Items","NMT Customization Kits","Fish Baits","Pitfall Seeds","Zodiac Items","Seasonal Items","Gulliver Items","Trash Items","Hello Kitty","Gullivar Items","Mermaid Items","Moms Items","Flowers","Bug Off Fishing Tourney Gift Wrapper","Sanrio Items","Updated Items","Golden Tools","Simple Panels","Turnips","Villagers in Boxes"],"visitors":0,"cat":"public","theme":"teal","description":"Summer-themed treasure island featuring beach items, seasonal furniture, and warm-weather collectibles.","dodo_code":"GETTIN"},
    {"id":"bathala","name":"BATHALA","status":"ONLINE","type":"2.0 Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":4,"cat":"public","theme":"teal","description":"Full 2.0 treasure island featuring vehicles, vines, moss, cooking items, and modern furniture.","dodo_code":"GR643"},
    {"id":"kaulayaw","name":"KAULAYAW","status":"ONLINE","type":"2.0 Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":3,"cat":"public","theme":"teal","description":"2.0-focused island centered on cooking, food items, and contemporary furniture pieces.","dodo_code":"2PYP0"},
    {"id":"tadhana","name":"TADHANA","status":"ONLINE","type":"Furniture Island","seasonal":"Year-Round","items":["Bathroom Appliances","Home Appliances","Electronics","Beds","Tables","Chairs & Sofas","Toys","Vanity Mirror","Home Decor","Outdoor Decor","Plants","Foods","Lamps & Lights","Garden Items","Garden Decor","Camping Items","Beach Items","Nooks Sports Items","Musical Instruments","Turnips","Villagers in Boxes"],"visitors":1,"cat":"public","theme":"purple","description":"Furniture island featuring popular themed sets and a wide range of stylish home decor.","dodo_code":"77KB4"},
    {"id":"pagsuyo","name":"PAGSUYO","status":"ONLINE","type":"Critters & DIY","seasonal":"Year-Round","items":["Critters","Shrubs / Bush","Flowers","Complete DIY 1","Complete DIY 2","Mats","K.K. Slider","Kicks Items","Musical Instruments","Fences","Fossils","Bookshelf","Manhole Cover","Floor Lights","Special Character Posters","Paintings and Statues","Sahara Items","Complete Wrapping Paper","All House Plant Items","Villager Photos","Nook Miles Ticket","Updated Items","Royal Crown","Trash","Bugs and Fish Models","Simple Panels","Seasonal Nook Items","Turnips","Villagers in Boxes"],"visitors":4,"cat":"public","theme":"teal","description":"Dedicated island for fish and bug models plus genuine and decorative art.","dodo_code":"G2BBV"},
    {"id":"kalawakan","name":"KALAWAKAN","status":"ONLINE","type":"1.0 Treasure Island","seasonal":"Year-Round","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Ratan Set","Robot Heroes","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Butterfly Models","Wedding Items","Celeste Items","Bunny Day Items","K.K. Slider Songs","Library Items","Mats","Kitchen Items","NMT Customization Kits","Fish Baits","Pitfall Seeds","Zodiac Items","Seasonal Items","Gulliver Items","Trash Items","Hello Kitty","Gullivar Items","Mermaid Items","Moms Items","Flowers","Bug Off Fishing Tourney Gift Wrapper","Sanrio Items","Updated Items","Golden Tools","Simple Panels","Turnips","Villagers in Boxes"],"visitors":3,"cat":"public","theme":"teal","description":"Retro-inspired island with rattan, diner, and throwback furniture sets.","dodo_code":"671SW"},
    {"id":"dalangin","name":"DALANGIN","status":"ONLINE","type":"2.0 Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":6,"cat":"public","theme":"teal","description":"2.0 island featuring gyroids, new furniture, and shopping-related items.","dodo_code":"2S7Y5"},
    {"id":"pagsamo","name":"PAGSAMO","status":"ONLINE","type":"Furniture Island","seasonal":"Year-Round","items":["Bathroom Appliances","Home Appliances","Electronics","Beds","Tables","Chairs & Sofas","Toys","Vanity Mirror","Home Decor","Outdoor Decor","Plants","Foods","Lamps & Lights","Garden Items","Garden Decor","Camping Items","Beach Items","Nooks Sports Items","Musical Instruments","Turnips","Villagers in Boxes"],"visitors":5,"cat":"public","theme":"purple","description":"Furniture island focused on elegant, nordic, and ranch-style aesthetics.","dodo_code":"C1DXQ"},
    {"id":"tala","name":"TALA","status":"ONLINE","type":"Materials and DIY","seasonal":"Year-Round","items":["DIY Equipment","DIY Housewares","Fish","Sea Creatures","Bugs","Full Grown Trees","Flowers","Flower Seeds","Bush","DIY Miscellaneous","DIY Rug","Wallpaper","Flooring","DIY Tools","Wall Mounted Items","Fence","DIY Savory & Sweets","Gyroids","Wallpaper & Flooring","Wrapping Paper","NMT","Bells","Royal Crown","Turnips","Villagers in Boxes"],"visitors":5,"cat":"public","theme":"teal","description":"Material island stocked with crafting resources and star fragments.","dodo_code":"8LVHT"},
    {"id":"matahom","name":"MATAHOM","status":"ONLINE","type":"Clothing Island","seasonal":"Spring","items":["All Tops","All Bottoms","All Accessories","All Bags","All Headwear","All Dress-Up","All Shoes","Umbrellas","Turnips","Villagers in Boxes"],"visitors":3,"cat":"public","theme":"pink","description":"Fashion island offering traditional and modern clothing accessories.","dodo_code":"LGCWJ"},
    {"id":"kundiman","name":"KUNDIMAN","status":"ONLINE","type":"1.0 Treasure Island","seasonal":"Year-Round","items":["Antique Set","Imperial Set","Cute Set","Diner Set","Ratan Set","Robot Heroes","Nook Miles Redemption","Pocket Camp Items","Birthday Items","Butterfly Models","Wedding Items","Celeste Items","Bunny Day Items","K.K. Slider Songs","Library Items","Mats","Kitchen Items","Zodiac Items","Seasonal Items","Moms Items","Flowers","Bug Off","Fishing Tourney","Gift Wrapper","Sanrio Items","Updated Items","Golden Tools","Simple Panels","Turnips","Villagers in Boxes","NMT","Customization Kits","Fish Baits","Pitfall Seeds","Gulliver Items","Trash Items","Hello Kitty","Gullivar Items","Mermaid Items"],"visitors":3,"cat":"public","theme":"teal","description":"Classic island for complete 1.0 furniture sets and interior walls and floors.","dodo_code":"3F3SR"},
    {"id":"gunita","name":"GUNITA","status":"ONLINE","type":"2.0 Treasure Island","seasonal":"Year-Round","items":["Random 2.0 Items","New DIYs","2.0 Villager Photos and Posters","New Vehicles","Old and New Materials","Cool New Items","Gyroids","New Food Items","2.0 Fence","2.0 Nook Miles Redemption","2.0 Furnitures","Villagers in Boxes","Turnips"],"visitors":5,"cat":"public","theme":"teal","description":"2.0 treasure island featuring the latest items, gyroids, and food items.","dodo_code":"2XBP1"},
    {"id":"silakbo","name":"SILAKBO","status":"ONLINE","type":"Seasonal Items","seasonal":"Halloween","items":["Cherry Blossom Items","Young Spring Bamboo Items","Summer Items","Tree's Bounty Items","Maple Leaf Items","Mush Items","Ice Furniture","Christmas (Festive) Items","January Seasonal Items","February Seasonal Items","March Seasonal Items","April Seasonal Items","May Seasonal Items","June Seasonal Items","July Seasonal Items","August Items","September Items","October & November Seasonal Items","December Seasonal Items","Bunny Day Items","Festivale Items","Materials","Golden Tools","Nook Miles Ticket","Turnips","Villagers in Boxes"],"visitors":2,"cat":"public","theme":"teal","description":"Halloween-themed island with spooky furniture and seasonal treats.","dodo_code":"332CR"},
    {"id":"sinagtala","name":"SINAGTALA","status":"ONLINE","type":"Materials and DIY","seasonal":"Year-Round","items":["DIY Equipment","DIY Housewares","Fish","Sea Creatures","Bugs","Full Grown Trees","Flowers","Flower Seeds","Bush","DIY Miscellaneous","DIY Rug","Wallpaper","Flooring","DIY Tools","Wall Mounted Items","Fence","DIY Savory & Sweets","Gyroids","Wallpaper & Flooring","Wrapping Paper","NMT","Bells","Royal Crown","Turnips","Villagers in Boxes"],"visitors":0,"cat":"public","theme":"teal","description":"Celestial materials island featuring star fragments and seasonal crafting mats.","dodo_code":"NNK75"},
    {"id":"paraluman","name":"PARALUMAN","status":"ONLINE","type":"Clothing Island","seasonal":"Winter","items":["All Tops","All Bottoms","All Accessories","All Bags","All Headwear","All Dress-Up","All Shoes","Umbrellas","Turnips","Villagers in Boxes"],"visitors":6,"cat":"public","theme":"pink","description":"Cold-weather fashion island with coats, boots, and stylish headwear.","dodo_code":"FNKT6"},
    {"id":"amihan","name":"AMIHAN","status":"ONLINE","type":"Seasonal Items","seasonal":"Festive","items":["Cherry Blossom Items","Young Spring Bamboo Items","Summer Items","Tree's Bounty Items","Maple Leaf Items","Mush Items","Ice Furniture","Christmas (Festive) Items","January Seasonal Items","February Seasonal Items","March Seasonal Items","April Seasonal Items","May Seasonal Items","June Seasonal Items","July Seasonal Items","August Items","September Items","October & November Seasonal Items","December Seasonal Items","Bunny Day Items","Festivale Items","Materials","Golden Tools","Nook Miles Ticket","Turnips","Villagers in Boxes"],"visitors":1,"cat":"public","theme":"teal","description":"Winter and holiday island featuring festive decorations and Toy Day items.","dodo_code":"9NK7J"},
    {"id":"babaylan","name":"BABAYLAN","status":"ONLINE","type":"Seasonal Items","seasonal":"Cherry Blossom","items":["Cherry Blossom Items","Young Spring Bamboo Items","Summer Items","Tree's Bounty Items","Maple Leaf Items","Mush Items","Ice Furniture","Christmas (Festive) Items","January Seasonal Items","February Seasonal Items","March Seasonal Items","April Seasonal Items","May Seasonal Items","June Seasonal Items","July Seasonal Items","August Items","September Items","October & November Seasonal Items","December Seasonal Items","Bunny Day Items","Festivale Items","Materials","Golden Tools","Nook Miles Ticket","Turnips","Villagers in Boxes"],"visitors":2,"cat":"public","theme":"teal","description":"Spring-themed island with cherry-blossom petals, bonsai, and branches.","dodo_code":"17YMJ"},
    {"id":"dakila","name":"DAKILA","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive themed island featuring Zelda, Splatoon, Lego, and various aesthetic furniture sets.","dodo_code":None},
    {"id":"kalangitan","name":"KALANGITAN","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive themed island featuring Zelda, Splatoon, Lego, and various aesthetic furniture sets.","dodo_code":None},
    {"id":"malaya","name":"MALAYA","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive themed island featuring Zelda, Splatoon, Lego, and various aesthetic furniture sets.","dodo_code":None},
    {"id":"pangarap","name":"PANGARAP","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive themed island featuring Zelda, Splatoon, Lego, and various aesthetic furniture sets.","dodo_code":None},
    {"id":"dangal","name":"DANGAL","status":"ONLINE","type":"3.0 Themed Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":4,"cat":"public","theme":"teal","description":"3.0 update island featuring Zelda, Splatoon, Lego, and specific aesthetic themes.","dodo_code":"JQ1KB"},
    {"id":"kariktan","name":"KARIKTAN","status":"ONLINE","type":"3.0 Themed Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":3,"cat":"public","theme":"teal","description":"3.0 update island featuring Zelda, Splatoon, Lego, and specific aesthetic themes.","dodo_code":"33W2J"},
    {"id":"banaag","name":"BANAAG","status":"ONLINE","type":"3.0 Themed Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":2,"cat":"public","theme":"teal","description":"3.0 update island featuring Zelda, Splatoon, Lego, and specific aesthetic themes.","dodo_code":"LXXQW"},
    {"id":"sinag","name":"SINAG","status":"ONLINE","type":"3.0 Themed Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":6,"cat":"public","theme":"teal","description":"3.0 update island featuring Zelda, Splatoon, Lego, and specific aesthetic themes.","dodo_code":"3D7L6"},
    {"id":"giting","name":"GITING","status":"ONLINE","type":"3.0 Themed Island","seasonal":"Year-Round","items":["Zelda Theme","Splatoon Theme","Lego Theme","Mario / Tubular Theme","Hotel Theme","Marble Theme","Kiddie Theme","Artful Theme","Standalone Items","Turnips","Villagers"],"visitors":4,"cat":"public","theme":"teal","description":"3.0 update island featuring Zelda, Splatoon, Lego, and specific aesthetic themes.","dodo_code":"CG3PD"},
    {"id":"diwa","name":"DIWA","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["Critters","Shrubs & Bushes","Flowers","Complete DIY Set 1","Complete DIY Set 2","Materials","Kicks Items","K.K. Slider Songs","Musical Instruments","Fences","Fossils","Bookshelf / Manhole Covers / Floor Lights","Special Character Posters","Saharah Items","Paintings & Statues","Complete Wrapping Paper","All House Plant Items","Villager Photos","Nook Miles Tickets","Updated Items","Turnips","Royal Crown","Trash Items","Bugs & Fish Models","Simple Panels","Seasonal Nook Items","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive island with fully organized item categories including DIY sets, materials, furniture collections, posters, fossils, models, and villagers in boxes.","dodo_code":None},
    {"id":"gabay","name":"GABAY","status":"SUB ONLY","type":"Themed Treasure Island","seasonal":"Year-Round","items":["All Tops","All Bottoms","All Dress-Up","All Shoes","All Headwear","All Accessories","All Bags","Umbrellas","Turnips","Villagers in Boxes"],"visitors":0,"cat":"member","theme":"gold","description":"Subscriber-exclusive clothing island featuring fully organized wearables including tops, bottoms, dress-up outfits, shoes, headwear, accessories, bags, and umbrellas.","dodo_code":None},
    {"id":"tinig","name":"TINIG","status":"ONLINE","type":"Materials and DIY","seasonal":"Year-Round","items":["DIY Equipment","DIY Housewares","Fish","Sea Creatures","Bugs","Full Grown Trees","Flowers","Flower Seeds","Bush","DIY Miscellaneous","DIY Rug","Wallpaper","Flooring","DIY Tools","Wall Mounted Items","Fence","DIY Savory & Sweets","Gyroids","Wallpaper & Flooring","Wrapping Paper","NMT","Bells","Royal Crown","Turnips","Villagers in Boxes"],"visitors":6,"cat":"public","theme":"teal","description":"Celestial materials island featuring star fragments and seasonal crafting mats.","dodo_code":"GDY4M"},
    {"id":"marilag","name":"MARILAG","status":"ONLINE","type":"Clothing Island","seasonal":"Spring","items":["All Tops","All Bottoms","All Accessories","All Bags","All Headwear","All Dress-Up","All Shoes","Umbrellas","Turnips","Villagers in Boxes"],"visitors":0,"cat":"public","theme":"pink","description":"Fashion island offering traditional and modern clothing accessories.","dodo_code":"745QL"},
]


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------
def get_db():
    """Return a synchronous SQLite connection to chobot.db."""
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_dashboard_db():
    """Create / migrate dashboard-specific tables and seed island data."""
    try:
        conn = get_db()

        # Full IslandData-compatible table
        conn.execute("""
            CREATE TABLE IF NOT EXISTS islands (
                id          TEXT PRIMARY KEY,
                name        TEXT NOT NULL,
                type        TEXT NOT NULL DEFAULT '',
                items       TEXT NOT NULL DEFAULT '[]',
                theme       TEXT NOT NULL DEFAULT 'teal',
                cat         TEXT NOT NULL DEFAULT 'public',
                description TEXT NOT NULL DEFAULT '',
                seasonal    TEXT NOT NULL DEFAULT '',
                status      TEXT NOT NULL DEFAULT 'OFFLINE',
                visitors    INTEGER NOT NULL DEFAULT 0,
                dodo_code   TEXT,
                map_url     TEXT,
                updated_at  TEXT
            )
        """)

        # Legacy table kept for backward compatibility
        conn.execute("""
            CREATE TABLE IF NOT EXISTS island_metadata (
                name       TEXT PRIMARY KEY,
                category   TEXT NOT NULL DEFAULT 'public',
                theme      TEXT NOT NULL DEFAULT 'teal',
                notes      TEXT NOT NULL DEFAULT '',
                updated_at TEXT
            )
        """)

        # Seed all 47 islands (INSERT OR IGNORE — never overwrites manual edits)
        now = datetime.now(timezone.utc).isoformat()
        for isl in _SAMPLE_ISLANDS:
            conn.execute(
                """INSERT OR IGNORE INTO islands
                       (id, name, type, items, theme, cat, description, seasonal,
                        status, visitors, dodo_code, map_url, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    isl["id"], isl["name"], isl["type"],
                    json.dumps(isl["items"]),
                    isl["theme"], isl["cat"], isl["description"], isl["seasonal"],
                    isl["status"], isl["visitors"], isl.get("dodo_code"), None, now,
                ),
            )

        conn.commit()
        conn.close()
        logger.info("Dashboard DB initialised — %d seed islands available", len(_SAMPLE_ISLANDS))
    except sqlite3.Error as exc:
        logger.warning("Could not initialise dashboard DB: %s", exc)


# ---------------------------------------------------------------------------
# R2 / S3 helpers
# ---------------------------------------------------------------------------
def _get_r2_client():
    """Return a boto3 S3 client pointed at Cloudflare R2, or None if unconfigured."""
    if not (Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY):
        return None
    endpoint = f"https://{Config.R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=Config.R2_ACCESS_KEY_ID,
        aws_secret_access_key=Config.R2_SECRET_ACCESS_KEY,
        config=BotocoreConfig(signature_version="s3v4"),
        region_name="auto",
    )


def _upload_map_to_r2(file_bytes: bytes, content_type: str, island_id: str) -> str:
    """Upload map image bytes to R2 and return the public URL."""
    client = _get_r2_client()
    if client is None:
        raise RuntimeError(
            "R2 is not configured — set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, "
            "R2_SECRET_ACCESS_KEY, R2_BUCKET_NAME, and R2_PUBLIC_URL in .env"
        )
    ext = mimetypes.guess_extension(content_type) or ".png"
    ext = {".jpe": ".jpg", ".jfif": ".jpg"}.get(ext, ext)
    key = f"maps/{island_id}{ext}"

    # Delete any pre-existing map files for this island (different extension)
    existing = client.list_objects_v2(
        Bucket=Config.R2_BUCKET_NAME,
        Prefix=f"maps/{island_id}",
    )
    for obj in existing.get("Contents", []):
        if obj["Key"] != key:
            client.delete_object(Bucket=Config.R2_BUCKET_NAME, Key=obj["Key"])

    client.put_object(
        Bucket=Config.R2_BUCKET_NAME,
        Key=key,
        Body=file_bytes,
        ContentType=content_type,
    )
    base = Config.R2_PUBLIC_URL.rstrip("/")
    return f"{base}/{key}"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------
def _check_session():
    return bool(session.get("mod_logged_in"))


def _check_bearer():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer ") and Config.DASHBOARD_SECRET:
        return auth[len("Bearer "):] == Config.DASHBOARD_SECRET
    return False


def login_required(f):
    """Decorator for web routes — redirects to /dashboard/login if not authenticated."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_session():
            return redirect(url_for("dashboard.login"))
        return f(*args, **kwargs)
    return _decorated


def api_auth_required(f):
    """Decorator for JSON API routes — returns 401 when token/session is missing."""
    @wraps(f)
    def _decorated(*args, **kwargs):
        if not _check_bearer() and not _check_session():
            return jsonify({"error": "Unauthorized — send 'Authorization: Bearer <DASHBOARD_SECRET>'"}), 401
        return f(*args, **kwargs)
    return _decorated


# ---------------------------------------------------------------------------
# Filesystem helpers
# ---------------------------------------------------------------------------
def _read_file(folder_path, filename):
    try:
        with open(os.path.join(folder_path, filename), "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except (FileNotFoundError, IOError, UnicodeDecodeError):
        return None


def _write_file(folder_path, filename, content):
    with open(os.path.join(folder_path, filename), "w", encoding="utf-8") as fh:
        fh.write(content)


def _collect_fs_islands():
    """Return a dict keyed by uppercase island name with live filesystem data."""
    result = {}

    def _scan(directory, itype):
        if not directory or not os.path.exists(directory):
            return
        with os.scandir(directory) as entries:
            for entry in entries:
                if entry.is_dir():
                    uname = entry.name.upper()
                    result[uname] = {
                        "name":        uname,
                        "fs_path":     entry.path,
                        "fs_type":     itype,
                        "fs_dodo":     _read_file(entry.path, "Dodo.txt"),
                        "fs_visitors": _read_file(entry.path, "Visitors.txt"),
                    }

    _scan(Config.DIR_FREE, "Free")
    _scan(Config.DIR_VIP,  "VIP")
    return result


def _ts_to_str(ts):
    """Convert a Unix timestamp int to a human-readable UTC string."""
    if ts is None:
        return "\u2014"
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, OverflowError, OSError):
        return str(ts)


def _where_clause(conditions: list) -> str:
    """Build a safe WHERE clause from a list of predefined SQL fragment strings.

    Only hardcoded SQL condition strings (containing '?' placeholders) may be
    passed here — never raw user input.  User-supplied values must be passed
    separately as a params list to the db.execute() call.
    """
    return ("WHERE " + " AND ".join(conditions)) if conditions else ""


def _row_to_island_dict(row: dict) -> dict:
    """Decode the items JSON column and return a plain dict."""
    try:
        row["items"] = json.loads(row.get("items") or "[]")
    except (ValueError, TypeError):
        row["items"] = []
    return row


def _merge_island(db_row: dict, fs: dict | None) -> dict:
    """Overlay live filesystem data (Dodo / Visitors) onto a DB island record."""
    db_row["fs_dodo"]     = fs["fs_dodo"]     if fs else None
    db_row["fs_visitors"] = fs["fs_visitors"] if fs else None
    db_row["fs_type"]     = fs["fs_type"]     if fs else None
    db_row["fs_path"]     = fs["fs_path"]     if fs else None
    return db_row


# ===========================================================================
# WEB ROUTES
# ===========================================================================

@dashboard.route("/login", methods=["GET", "POST"])
def login():
    if _check_session():
        return redirect(url_for("dashboard.index"))
    if request.method == "POST":
        secret = request.form.get("secret", "")
        if secret and Config.DASHBOARD_SECRET and secret == Config.DASHBOARD_SECRET:
            session["mod_logged_in"] = True
            session.permanent = True
            return redirect(url_for("dashboard.index"))
        flash("Invalid secret key. Please try again.", "error")
    return render_template("dashboard/login.html")


@dashboard.route("/logout")
def logout():
    session.pop("mod_logged_in", None)
    return redirect(url_for("dashboard.login"))


@dashboard.route("/")
@login_required
def index():
    db = get_db()
    try:
        total_visits   = db.execute("SELECT COUNT(*) FROM island_visits").fetchone()[0]
        total_warnings = db.execute("SELECT COUNT(*) FROM warnings").fetchone()[0]
        recent_raw     = db.execute(
            "SELECT ign, destination, authorized, timestamp "
            "FROM island_visits ORDER BY timestamp DESC LIMIT 10"
        ).fetchall()
    except sqlite3.Error:
        total_visits = total_warnings = 0
        recent_raw = []
    finally:
        db.close()

    recent = [
        {
            "ign":         r["ign"],
            "destination": r["destination"],
            "authorized":  bool(r["authorized"]),
            "timestamp":   _ts_to_str(r["timestamp"]),
        }
        for r in recent_raw
    ]

    db2 = get_db()
    try:
        island_count = db2.execute("SELECT COUNT(*) FROM islands").fetchone()[0]
    except sqlite3.Error:
        island_count = 0
    finally:
        db2.close()

    return render_template(
        "dashboard/index.html",
        total_visits=total_visits,
        total_warnings=total_warnings,
        recent=recent,
        island_count=island_count,
    )


@dashboard.route("/islands")
@login_required
def islands():
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except sqlite3.Error:
        db_islands = []
    finally:
        db.close()

    fs_map     = _collect_fs_islands()
    merged     = []
    seen_names = set()

    for isl in db_islands:
        uname = isl["name"].upper()
        seen_names.add(uname)
        merged.append(_merge_island(isl, fs_map.get(uname)))

    # Islands on filesystem but not yet in DB
    for uname, fs in fs_map.items():
        if uname not in seen_names:
            stub = {
                "id": uname.lower(), "name": uname, "type": "", "items": [],
                "theme": "teal", "cat": "public", "description": "", "seasonal": "",
                "status": "OFFLINE", "visitors": 0, "dodo_code": None,
                "map_url": None, "updated_at": None,
            }
            merged.append(_merge_island(stub, fs))

    merged.sort(key=lambda x: x["name"])
    return render_template("dashboard/islands.html", islands=merged)


@dashboard.route("/islands/<name>", methods=["GET", "POST"])
@login_required
def island_detail(name):
    island_id = name.lower()
    upper     = name.upper()

    db = get_db()
    try:
        row  = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
        meta = _row_to_island_dict(dict(row)) if row else None
    finally:
        db.close()

    # Locate filesystem path
    fs_path = fs_type = None
    for directory, itype in [(Config.DIR_FREE, "Free"), (Config.DIR_VIP, "VIP")]:
        if not directory:
            continue
        for candidate_name in [upper, name]:
            candidate = os.path.join(directory, candidate_name)
            if os.path.isdir(candidate):
                fs_path, fs_type = candidate, itype
                break
        if fs_path:
            break

    if request.method == "POST":
        isl_type         = request.form.get("type", "").strip()
        isl_seasonal     = request.form.get("seasonal", "").strip()
        isl_desc         = request.form.get("description", "").strip()
        isl_cat          = request.form.get("cat", "public")
        isl_theme        = request.form.get("theme", "teal")
        isl_status       = request.form.get("status", "OFFLINE")
        isl_dodo         = meta["dodo_code"] if meta else (_read_file(fs_path, "Dodo.txt") if fs_path else None)
        _fs_visitors_raw = _read_file(fs_path, "Visitors.txt") if not meta and fs_path else None
        isl_visitors_raw = str(meta["visitors"]) if meta else (_fs_visitors_raw or "0")

        # items come as a JSON array from the hidden input
        items_raw = request.form.get("items_json", "") or request.form.get("items", "")
        try:
            items_list = json.loads(items_raw) if items_raw.startswith("[") else [
                i.strip() for i in items_raw.split(",") if i.strip()
            ]
        except (ValueError, TypeError):
            items_list = []

        errors = []
        if isl_cat    not in ALLOWED_CATEGORIES: errors.append("Invalid category.")
        if isl_theme  not in ALLOWED_THEMES:     errors.append("Invalid theme.")
        if isl_status not in ALLOWED_STATUSES:   errors.append("Invalid status.")

        try:
            isl_visitors = int(isl_visitors_raw)
        except ValueError:
            isl_visitors = 0

        if errors:
            for e in errors:
                flash(e, "error")
        else:
            # dodo_code and visitors are managed by island bots; do not write to filesystem

            db2 = get_db()
            try:
                db2.execute(
                    """INSERT INTO islands
                           (id, name, type, items, theme, cat, description, seasonal,
                            status, visitors, dodo_code, map_url, updated_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                           name=excluded.name, type=excluded.type, items=excluded.items,
                           theme=excluded.theme, cat=excluded.cat,
                           description=excluded.description, seasonal=excluded.seasonal,
                           status=excluded.status, visitors=excluded.visitors,
                           dodo_code=excluded.dodo_code, updated_at=excluded.updated_at""",
                    (
                        island_id, upper, isl_type, json.dumps(items_list),
                        isl_theme, isl_cat, isl_desc, isl_seasonal,
                        isl_status, isl_visitors, isl_dodo,
                        meta["map_url"] if meta else None,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db2.commit()
            finally:
                db2.close()

            flash(f'Island "{upper}" saved successfully.', "success")
            return redirect(url_for("dashboard.islands"))

    island = meta or {
        "id": island_id, "name": upper, "type": "", "items": [],
        "theme": "teal", "cat": "public", "description": "", "seasonal": "",
        "status": "OFFLINE", "visitors": 0, "dodo_code": None,
        "map_url": None, "updated_at": None,
    }
    island["fs_path"]     = fs_path
    island["fs_type"]     = fs_type
    island["fs_dodo"]     = _read_file(fs_path, "Dodo.txt")     if fs_path else None
    island["fs_visitors"] = _read_file(fs_path, "Visitors.txt") if fs_path else None
    island["items_text"]  = ", ".join(island["items"]) if isinstance(island.get("items"), list) else ""

    r2_configured = bool(Config.R2_ACCOUNT_ID and Config.R2_ACCESS_KEY_ID and Config.R2_SECRET_ACCESS_KEY)

    return render_template(
        "dashboard/island_detail.html",
        island=island,
        allowed_categories=ALLOWED_CATEGORIES,
        allowed_themes=ALLOWED_THEMES,
        allowed_statuses=ALLOWED_STATUSES,
        r2_configured=r2_configured,
    )


@dashboard.route("/logs")
@login_required
def logs():
    page              = request.args.get("page", 1, type=int)
    per_page          = 25
    island_filter     = request.args.get("island", "").strip()
    authorized_filter = request.args.get("authorized", "")
    log_type          = request.args.get("type", "flights")

    db = get_db()
    try:
        if log_type == "warnings":
            conditions, params = [], []
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM warnings {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT w.*, iv.ign, iv.destination "
                f"FROM warnings w "
                f"LEFT JOIN island_visits iv ON w.visit_id = iv.id "
                f"{where} ORDER BY w.timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            entries = [
                {
                    "user_id":     r["user_id"],
                    "reason":      r["reason"],
                    "mod_id":      r["mod_id"],
                    "timestamp":   _ts_to_str(r["timestamp"]),
                    "ign":         r["ign"],
                    "destination": r["destination"],
                }
                for r in rows
            ]
        else:
            conditions, params = [], []
            if island_filter:
                conditions.append("destination LIKE ?")
                params.append(f"%{island_filter}%")
            if authorized_filter in ("0", "1"):
                conditions.append("authorized = ?")
                params.append(int(authorized_filter))
            where = _where_clause(conditions)
            total = db.execute(
                f"SELECT COUNT(*) FROM island_visits {where}", params
            ).fetchone()[0]
            rows = db.execute(
                f"SELECT * FROM island_visits {where} "
                f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [per_page, (page - 1) * per_page],
            ).fetchall()
            entries = [
                {
                    "id":            r["id"],
                    "ign":           r["ign"],
                    "origin_island": r["origin_island"],
                    "destination":   r["destination"],
                    "authorized":    bool(r["authorized"]),
                    "timestamp":     _ts_to_str(r["timestamp"]),
                }
                for r in rows
            ]
    except sqlite3.Error:
        total, entries = 0, []
    finally:
        db.close()

    return render_template(
        "dashboard/logs.html",
        entries=entries,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=max(1, (total + per_page - 1) // per_page),
        island_filter=island_filter,
        authorized_filter=authorized_filter,
        log_type=log_type,
    )


@dashboard.route("/analytics")
@login_required
def analytics():
    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        visits_by_day = [
            dict(r) for r in db.execute(
                "SELECT DATE(timestamp, 'unixepoch') AS day, COUNT(*) AS count "
                "FROM island_visits "
                "WHERE timestamp > strftime('%s','now','-7 days') "
                "GROUP BY day ORDER BY day"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
    except sqlite3.Error:
        top_islands = top_travelers = visits_by_day = []
        auth_raw = []
    finally:
        db.close()

    auth_map   = {r["authorized"]: r["count"] for r in auth_raw}
    auth_stats = {"authorized": auth_map.get(1, 0), "unauthorized": auth_map.get(0, 0)}

    return render_template(
        "dashboard/analytics.html",
        top_islands=top_islands,
        top_travelers=top_travelers,
        visits_by_day=visits_by_day,
        auth_stats=auth_stats,
    )


# ===========================================================================
# JSON CRUD API  (Bearer token OR active browser session)
# ===========================================================================

@dashboard.route("/api/islands", methods=["GET"])
@api_auth_required
def api_islands_list():
    """List all islands (DB records merged with live filesystem data)."""
    db = get_db()
    try:
        rows       = db.execute("SELECT * FROM islands ORDER BY name").fetchall()
        db_islands = [_row_to_island_dict(dict(r)) for r in rows]
    except sqlite3.Error:
        db_islands = []
    finally:
        db.close()

    fs_map  = _collect_fs_islands()
    result  = []
    seen    = set()
    for isl in db_islands:
        uname = isl["name"].upper()
        seen.add(uname)
        result.append(_merge_island(isl, fs_map.get(uname)))
    for uname, fs in fs_map.items():
        if uname not in seen:
            stub = {
                "id": uname.lower(), "name": uname, "type": "", "items": [],
                "theme": "teal", "cat": "public", "description": "", "seasonal": "",
                "status": "OFFLINE", "visitors": 0, "dodo_code": None,
                "map_url": None, "updated_at": None,
            }
            result.append(_merge_island(stub, fs))
    result.sort(key=lambda x: x["name"])
    return jsonify(result)


@dashboard.route("/api/islands", methods=["POST"])
@api_auth_required
def api_island_create():
    """Create or upsert a full island record."""
    data      = request.get_json(silent=True) or {}
    island_id = (data.get("id") or data.get("name", "")).strip().lower()
    name      = (data.get("name") or island_id).strip().upper()
    isl_type  = data.get("type", "")
    items     = data.get("items", [])
    theme     = data.get("theme", "teal")
    cat       = data.get("cat", "public")
    desc      = data.get("description", "")
    seasonal  = data.get("seasonal", "")
    status    = data.get("status", "OFFLINE")
    visitors  = int(data.get("visitors", 0))
    dodo_code = data.get("dodoCode") or data.get("dodo_code") or None
    map_url   = data.get("mapUrl")   or data.get("map_url")   or None

    if not island_id:
        return jsonify({"error": "id or name is required"}), 400
    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    db = get_db()
    try:
        db.execute(
            """INSERT INTO islands
                   (id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (island_id, name, isl_type, json.dumps(items),
             theme, cat, desc, seasonal, status, visitors, dodo_code, map_url,
             datetime.now(timezone.utc).isoformat()),
        )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "ok", "id": island_id}), 201


@dashboard.route("/api/islands/<name>", methods=["GET"])
@api_auth_required
def api_island_get(name):
    """Get a single island record."""
    island_id = name.lower()
    db = get_db()
    try:
        row = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
    finally:
        db.close()
    if not row:
        return jsonify({"error": f'Island "{name}" not found'}), 404
    isl    = _row_to_island_dict(dict(row))
    fs_map = _collect_fs_islands()
    return jsonify(_merge_island(isl, fs_map.get(isl["name"].upper())))


@dashboard.route("/api/islands/<name>", methods=["PUT"])
@api_auth_required
def api_island_update(name):
    """Update a single island record (partial or full)."""
    island_id = name.lower()
    data      = request.get_json(silent=True) or {}

    db = get_db()
    try:
        row      = db.execute("SELECT * FROM islands WHERE id = ?", (island_id,)).fetchone()
        existing = _row_to_island_dict(dict(row)) if row else {}
    finally:
        db.close()

    cat    = data.get("cat",    existing.get("cat",    "public"))
    theme  = data.get("theme",  existing.get("theme",  "teal"))
    status = data.get("status", existing.get("status", "OFFLINE"))

    if cat    not in ALLOWED_CATEGORIES: return jsonify({"error": f"cat must be one of {ALLOWED_CATEGORIES}"}),  400
    if theme  not in ALLOWED_THEMES:     return jsonify({"error": f"theme must be one of {ALLOWED_THEMES}"}),    400
    if status not in ALLOWED_STATUSES:   return jsonify({"error": f"status must be one of {ALLOWED_STATUSES}"}), 400

    items_in = data.get("items", existing.get("items", []))
    if isinstance(items_in, str):
        try:
            items_in = json.loads(items_in)
        except ValueError:
            items_in = [i.strip() for i in items_in.split(",") if i.strip()]

    db2 = get_db()
    try:
        db2.execute(
            """INSERT INTO islands
                   (id, name, type, items, theme, cat, description, seasonal,
                    status, visitors, dodo_code, map_url, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                   name=excluded.name, type=excluded.type, items=excluded.items,
                   theme=excluded.theme, cat=excluded.cat, description=excluded.description,
                   seasonal=excluded.seasonal, status=excluded.status,
                   visitors=excluded.visitors, dodo_code=excluded.dodo_code,
                   updated_at=excluded.updated_at""",
            (
                island_id,
                data.get("name", existing.get("name", island_id.upper())).upper(),
                data.get("type",        existing.get("type",        "")),
                json.dumps(items_in),
                theme, cat,
                data.get("description", existing.get("description", "")),
                data.get("seasonal",    existing.get("seasonal",    "")),
                status,
                int(data.get("visitors", existing.get("visitors", 0))),
                data.get("dodoCode") or data.get("dodo_code") or existing.get("dodo_code"),
                existing.get("map_url"),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        db2.commit()
    finally:
        db2.close()
    return jsonify({"status": "ok", "id": island_id})


@dashboard.route("/api/islands/<name>", methods=["DELETE"])
@api_auth_required
def api_island_delete(name):
    """Delete stored metadata for an island (does not touch the filesystem)."""
    island_id = name.lower()
    db = get_db()
    try:
        db.execute("DELETE FROM islands WHERE id = ?", (island_id,))
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "deleted", "id": island_id})


@dashboard.route("/api/islands/<name>/map", methods=["POST"])
@api_auth_required
def api_island_upload_map(name):
    """Upload an island map image to Cloudflare R2 and store the URL."""
    island_id = name.lower()

    if "map" not in request.files:
        return jsonify({"error": "No file part named 'map'"}), 400
    file = request.files["map"]
    if not file or not file.filename:
        return jsonify({"error": "Empty filename"}), 400

    file_bytes = file.read()
    if len(file_bytes) > MAX_MAP_SIZE:
        return jsonify({"error": f"File too large (max {MAX_MAP_SIZE // 1024 // 1024} MB)"}), 413

    content_type = file.content_type or mimetypes.guess_type(file.filename)[0] or "image/png"
    if content_type not in ALLOWED_MAP_TYPES:
        return jsonify({"error": f"Unsupported type: {content_type}. Allowed: {sorted(ALLOWED_MAP_TYPES)}"}), 415

    try:
        map_url = _upload_map_to_r2(file_bytes, content_type, island_id)
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 503
    except (ClientError, NoCredentialsError) as exc:
        logger.error("R2 upload failed for island %s: %s", island_id, exc)
        return jsonify({"error": "R2 upload failed", "details": str(exc)}), 502

    db = get_db()
    try:
        db.execute(
            "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
            (map_url, datetime.now(timezone.utc).isoformat(), island_id),
        )
        if db.execute("SELECT changes()").fetchone()[0] == 0:
            db.execute(
                "INSERT INTO islands (id, name, map_url, updated_at) VALUES (?,?,?,?)",
                (island_id, island_id.upper(), map_url, datetime.now(timezone.utc).isoformat()),
            )
        db.commit()
    finally:
        db.close()
    return jsonify({"status": "uploaded", "id": island_id, "map_url": map_url})


@dashboard.route("/api/islands/sync-maps", methods=["POST"])
@api_auth_required
def api_sync_maps():
    """Scan the R2 bucket for existing map images and back-fill map_url in the DB.

    For every object under the ``maps/`` prefix in the configured R2 bucket,
    derive the island id from the object key (e.g. ``maps/alapaap.jpg``
    → island id ``alapaap``), construct the public URL, and write it into the
    ``islands`` table.  Rows that already have a ``map_url`` are also updated
    so that any manually renamed/re-uploaded files are corrected.

    Returns a JSON summary ``{"synced": N, "skipped": N, "errors": [...]}``.
    """
    client = _get_r2_client()
    if client is None:
        return jsonify({"error": "R2 is not configured"}), 503

    base = (Config.R2_PUBLIC_URL or "").rstrip("/")
    if not base:
        return jsonify({"error": "R2_PUBLIC_URL is not configured"}), 503

    # Collect all objects under maps/ prefix (handle paginated responses)
    keys: list[str] = []
    kwargs: dict = {"Bucket": Config.R2_BUCKET_NAME, "Prefix": "maps/"}
    while True:
        try:
            resp = client.list_objects_v2(**kwargs)
        except (ClientError, NoCredentialsError) as exc:
            return jsonify({"error": "R2 list failed", "details": str(exc)}), 502
        for obj in resp.get("Contents", []):
            keys.append(obj["Key"])
        if resp.get("IsTruncated"):
            kwargs["ContinuationToken"] = resp["NextContinuationToken"]
        else:
            break

    synced = 0
    skipped = 0
    errors: list[str] = []
    now = datetime.now(timezone.utc).isoformat()

    db = get_db()
    try:
        for key in keys:
            # key looks like "maps/alapaap.jpg" or "maps/subdirectory/..." – skip nested
            parts = key.split("/")
            if len(parts) != 2:
                skipped += 1
                continue
            filename = parts[1]
            if not filename:
                skipped += 1
                continue
            # Strip extension to get island id
            island_id = filename.rsplit(".", 1)[0].lower()
            if not island_id:
                skipped += 1
                continue
            map_url = f"{base}/{key}"
            try:
                db.execute(
                    "UPDATE islands SET map_url = ?, updated_at = ? WHERE id = ?",
                    (map_url, now, island_id),
                )
                if db.execute("SELECT changes()").fetchone()[0] == 0:
                    # Island row doesn't exist yet — create a minimal one
                    db.execute(
                        "INSERT OR IGNORE INTO islands (id, name, map_url, updated_at) "
                        "VALUES (?, ?, ?, ?)",
                        (island_id, island_id.upper(), map_url, now),
                    )
                synced += 1
            except sqlite3.Error as exc:
                errors.append(f"{island_id}: {exc}")
        db.commit()
    finally:
        db.close()

    return jsonify({"synced": synced, "skipped": skipped, "errors": errors})


@dashboard.route("/api/analytics", methods=["GET"])
@api_auth_required
def api_analytics():
    """Return analytics summary as JSON."""
    db = get_db()
    try:
        top_islands = [
            dict(r) for r in db.execute(
                "SELECT destination, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY destination "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        top_travelers = [
            dict(r) for r in db.execute(
                "SELECT ign, COUNT(*) AS visit_count "
                "FROM island_visits GROUP BY ign "
                "ORDER BY visit_count DESC LIMIT 10"
            ).fetchall()
        ]
        auth_raw = db.execute(
            "SELECT authorized, COUNT(*) AS count FROM island_visits GROUP BY authorized"
        ).fetchall()
    except sqlite3.Error:
        top_islands = top_travelers = []
        auth_raw = []
    finally:
        db.close()

    auth_map = {r["authorized"]: r["count"] for r in auth_raw}
    return jsonify({
        "top_islands":         top_islands,
        "top_travelers":       top_travelers,
        "authorized_visits":   auth_map.get(1, 0),
        "unauthorized_visits": auth_map.get(0, 0),
    })


@dashboard.route("/api/logs", methods=["GET"])
@api_auth_required
def api_logs():
    """Return paginated flight-log entries as JSON."""
    page          = request.args.get("page", 1, type=int)
    per_page      = min(request.args.get("per_page", 25, type=int), 100)
    island_filter = request.args.get("island", "").strip()

    db = get_db()
    try:
        conditions, params = [], []
        if island_filter:
            conditions.append("destination LIKE ?")
            params.append(f"%{island_filter}%")
        where = _where_clause(conditions)
        total = db.execute(
            f"SELECT COUNT(*) FROM island_visits {where}", params
        ).fetchone()[0]
        rows = db.execute(
            f"SELECT * FROM island_visits {where} "
            f"ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    except sqlite3.Error:
        total, rows = 0, []
    finally:
        db.close()

    return jsonify({
        "page":     page,
        "per_page": per_page,
        "total":    total,
        "entries":  [dict(r) for r in rows],
    })
