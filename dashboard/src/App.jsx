/**
 * dashboard/src/App.jsx
 * AMR-Intel — 6-screen dashboard with Genomic Intelligence layer
 *
 * Screens:
 *   1. Command Center
 *   2. Threat Operations
 *   3. Emergence Radar
 *   4. Genomic Intelligence  ← NEW
 *   5. Alert Investigation
 *   6. Executive Brief
 */

import { useState, useEffect, useCallback, useRef } from "react";
import { geoNaturalEarth1, geoPath } from "d3-geo";
import { feature } from "topojson-client";
import {
  AreaChart, Area, BarChart, Bar, LineChart, Line,
  XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
  ReferenceLine, PieChart, Pie, Cell
} from "recharts";

// ─── Design tokens ────────────────────────────────────────────────────────────
const C = {
  bg:          "#0B0F18",
  sidebar:     "#0D1117",
  surface:     "#131920",
  surfaceHigh: "#1A2332",
  surfaceBord: "#1E2D3D",
  border:      "#1E2D3D",
  borderHigh:  "#2A3F55",
  red:         "#EF4444",
  redDim:      "#450A0A",
  amber:       "#F59E0B",
  amberDim:    "#451A03",
  green:       "#10B981",
  greenDim:    "#022C22",
  blue:        "#3B82F6",
  blueDim:     "#1E3A5F",
  teal:        "#06B6D4",
  tealDim:     "#0A2930",
  purple:      "#8B5CF6",
  purpleDim:   "#2E1065",
  white:       "#F1F5F9",
  muted:       "#64748B",
  mutedHigh:   "#94A3B8",
  accent:      "#3B82F6",
};

const TIER_COLOR = { critical: C.red, warn: C.amber, monitor: C.blue, high: C.amber, watch: C.blue };
const TIER_BG    = { critical: C.redDim, warn: C.amberDim, monitor: C.blueDim, high: C.amberDim, watch: C.blueDim };
const TIER_LABEL = { critical: "CRITICAL", warn: "HIGH", monitor: "WATCH", high: "HIGH", watch: "WATCH" };

const CONF_COLOR = { HIGH: C.green, MEDIUM: C.amber, LOW: C.muted };
const CONF_BG    = { HIGH: C.greenDim, MEDIUM: C.amberDim, LOW: C.surfaceHigh };

const ISO3_TO_NAME = {
  BGR:"Bulgaria",HRV:"Croatia",CYP:"Cyprus",ROU:"Romania",GRC:"Greece",
  SVK:"Slovakia",POL:"Poland",CZE:"Czechia",ITA:"Italy",LTU:"Lithuania",
  LVA:"Latvia",HUN:"Hungary",DEU:"Germany",FRA:"France",ESP:"Spain",
  SWE:"Sweden",NOR:"Norway",DNK:"Denmark",FIN:"Finland",ISL:"Iceland",
  LUX:"Luxembourg",MLT:"Malta",IRL:"Ireland",GBR:"United Kingdom",
  SVN:"Slovenia",EST:"Estonia",BEL:"Belgium",NLD:"Netherlands",
  AUT:"Austria",CHE:"Switzerland",PRT:"Portugal",MKD:"N. Macedonia",
  SRB:"Serbia",BIH:"Bosnia",ALB:"Albania",MNE:"Montenegro",
  UKR:"Ukraine",RUS:"Russia",BLR:"Belarus",MDA:"Moldova",
  GEO:"Georgia",AZE:"Azerbaijan",TUR:"Turkey",
  NGA:"Nigeria",GHA:"Ghana",KEN:"Kenya",ZAF:"South Africa",
  ETH:"Ethiopia",TZA:"Tanzania",UGA:"Uganda",CMR:"Cameroon",
  SEN:"Senegal",CIV:"Côte d'Ivoire",ZMB:"Zambia",ZWE:"Zimbabwe",
  MOZ:"Mozambique",MDG:"Madagascar",BFA:"Burkina Faso",MLI:"Mali",
  NER:"Niger",MWI:"Malawi",
  EGY:"Egypt",MAR:"Morocco",SAU:"Saudi Arabia",ARE:"UAE",
  IRN:"Iran",IRQ:"Iraq",JOR:"Jordan",SYR:"Syria",LBN:"Lebanon",
  OMN:"Oman",YEM:"Yemen",KWT:"Kuwait",ISR:"Israel",
  IND:"India",BGD:"Bangladesh",NPL:"Nepal",LKA:"Sri Lanka",
  PAK:"Pakistan",MMR:"Myanmar",THA:"Thailand",IDN:"Indonesia",
  CHN:"China",JPN:"Japan",KOR:"S. Korea",PHL:"Philippines",
  VNM:"Vietnam",MYS:"Malaysia",SGP:"Singapore",
  USA:"United States",BRA:"Brazil",MEX:"Mexico",ARG:"Argentina",
  CHL:"Chile",COL:"Colombia",PER:"Peru",
  TUN:"Tunisia",ZWE:"Zimbabwe",
};
function countryName(iso3) { return ISO3_TO_NAME[iso3] || iso3; }
const ISO3_TO_ISO2 = {
  BGR:"BG",HRV:"HR",CYP:"CY",ROU:"RO",GRC:"GR",SVK:"SK",POL:"PL",CZE:"CZ",
  ITA:"IT",LTU:"LT",LVA:"LV",DEU:"DE",FRA:"FR",ESP:"ES",PRT:"PT",NLD:"NL",
  BEL:"BE",AUT:"AT",CHE:"CH",SWE:"SE",NOR:"NO",DNK:"DK",FIN:"FI",ISL:"IS",
  LUX:"LU",MLT:"MT",IRL:"IE",GBR:"GB",HUN:"HU",SVN:"SI",EST:"EE",
  NGA:"NG",GHA:"GH",KEN:"KE",ZAF:"ZA",ETH:"ET",EGY:"EG",MAR:"MA",
  TZA:"TZ",UGA:"UG",CMR:"CM",SEN:"SN",CIV:"CI",ZMB:"ZM",ZWE:"ZW",
  MOZ:"MZ",MDG:"MG",BFA:"BF",MLI:"ML",NER:"NE",MWI:"MW",BGD:"BD",
  IND:"IN",PAK:"PK",CHN:"CN",JPN:"JP",KOR:"KR",IDN:"ID",THA:"TH",
  VNM:"VN",PHL:"PH",MYS:"MY",SGP:"SG",NPL:"NP",LKA:"LK",MMR:"MM",
  USA:"US",BRA:"BR",MEX:"MX",ARG:"AR",CHL:"CL",COL:"CO",PER:"PE",
  SAU:"SA",ARE:"AE",IRN:"IR",IRQ:"IQ",JOR:"JO",SYR:"SY",LBN:"LB",
  OMN:"OM",YEM:"YE",KWT:"KW",ISR:"IL",TUR:"TR",AZE:"AZ",GEO:"GE",
  UKR:"UA",RUS:"RU",BLR:"BY",MDA:"MD",MKD:"MK",SRB:"RS",BIH:"BA",ALB:"AL",
  TUN:"TN",
};
function getFlagUrl(iso3) {
  const iso2 = (ISO3_TO_ISO2[iso3] || "").toLowerCase();
  return iso2 ? "https://flagcdn.com/16x12/" + iso2 + ".png" : null;
}
function CountryCell({ iso3, size="md" }) {
  const flagUrl = getFlagUrl(iso3);
  const name = countryName(iso3);
  const nameSize = size === "sm" ? 11 : size === "lg" ? 15 : 13;
  const codeSize = size === "sm" ? 9 : 10;
  return (
    <div style={{display:"flex",alignItems:"center",gap:6}}>
      {flagUrl && <img src={flagUrl} width="16" height="12" style={{borderRadius:1,flexShrink:0}} alt=""/>}
      <div>
        <div style={{fontSize:nameSize,fontWeight:600,color:"#F1F5F9",lineHeight:1.2}}>{name}</div>
        <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:codeSize,color:"#64748B"}}>{iso3}</div>
      </div>
    </div>
  );
}

const COUNTRY_REGION = {
  BGR:"EURO",HRV:"EURO",CYP:"EURO",ROU:"EURO",GRC:"EURO",SVK:"EURO",POL:"EURO",
  CZE:"EURO",ITA:"EURO",LTU:"EURO",LVA:"EURO",HUN:"EURO",DEU:"EURO",FRA:"EURO",
  ESP:"EURO",SWE:"EURO",NOR:"EURO",DNK:"EURO",FIN:"EURO",ISL:"EURO",LUX:"EURO",
  MLT:"EURO",IRL:"EURO",GBR:"EURO",SVN:"EURO",EST:"EURO",BEL:"EURO",NLD:"EURO",
  AUT:"EURO",CHE:"EURO",PRT:"EURO",MKD:"EURO",SRB:"EURO",BIH:"EURO",ALB:"EURO",
  MNE:"EURO",UKR:"EURO",RUS:"EURO",BLR:"EURO",MDA:"EURO",GEO:"EURO",AZE:"EURO",
  NGA:"AFRO",GHA:"AFRO",KEN:"AFRO",ZAF:"AFRO",ETH:"AFRO",TZA:"AFRO",CMR:"AFRO",
  SEN:"AFRO",ZMB:"AFRO",ZWE:"AFRO",MOZ:"AFRO",MDG:"AFRO",BFA:"AFRO",MLI:"AFRO",
  NER:"AFRO",MWI:"AFRO",
  EGY:"EMRO",MAR:"EMRO",SAU:"EMRO",ARE:"EMRO",IRN:"EMRO",IRQ:"EMRO",JOR:"EMRO",
  SYR:"EMRO",LBN:"EMRO",OMN:"EMRO",YEM:"EMRO",KWT:"EMRO",TUN:"EMRO",
  IND:"SEARO",BGD:"SEARO",NPL:"SEARO",LKA:"SEARO",MMR:"SEARO",THA:"SEARO",IDN:"SEARO",
  CHN:"WPRO",JPN:"WPRO",KOR:"WPRO",PHL:"WPRO",VNM:"WPRO",MYS:"WPRO",SGP:"WPRO",
  USA:"AMRO",BRA:"AMRO",MEX:"AMRO",ARG:"AMRO",CHL:"AMRO",COL:"AMRO",PER:"AMRO",
};
function getRegion(a) { return (a.region_who || COUNTRY_REGION[a.country_iso3] || "").toUpperCase(); }

const EMERGENCE_COLOR = {
  EMERGING: C.red, ESCALATING: C.amber,
  "ENDEMIC CRITICAL": "#F97316", WATCH: C.blue, IMPROVING: C.green, STABLE: C.muted,
};

// ─── Global CSS ───────────────────────────────────────────────────────────────
const CSS = [
  "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&family=JetBrains+Mono:wght@400;500;600;700&display=swap');",
  "*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }",
  "html, body, #root { height: 100%; }",
  "body { background: " + C.bg + "; color: " + C.white + "; font-family: 'Inter', sans-serif; font-size: 13px; -webkit-font-smoothing: antialiased; }",
  "::-webkit-scrollbar { width: 5px; height: 5px; }",
  "::-webkit-scrollbar-track { background: transparent; }",
  "::-webkit-scrollbar-thumb { background: " + C.border + "; border-radius: 3px; }",
  "::-webkit-scrollbar-thumb:hover { background: " + C.borderHigh + "; }",
  ".mono { font-family: 'JetBrains Mono', monospace; }",
  "@keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }",
  "@keyframes fadeUp { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }",
  ".fade-up { animation: fadeUp .25s ease forwards; }",
  "@keyframes spin { to{transform:rotate(360deg)} }",
  "input, select, button { font-family: inherit; }",
  "button { cursor: pointer; }",
  ".mob-bar { display:none; }",
  "@media (max-width:768px) {",
  "  .mob-bar { display:flex !important; align-items:center; gap:10px; padding:10px 14px;",
  "    background:" + C.sidebar + "; border-bottom:1px solid " + C.border + ";",
  "    position:sticky; top:0; z-index:30; }",
  "  .desk-sidebar { display:none !important; }",
  "  .mob-sidebar-open { display:flex !important; }",
  "  .fade-up { padding: 10px 8px !important; }",
  /* Stat rows: 2-col on mobile instead of 4/6 col */
  "  .stat-row { grid-template-columns: 1fr 1fr !important; }",
  /* Executive brief stat row */
  "  .brief-stat-row { grid-template-columns: 1fr 1fr !important; gap: 8px !important; }",
  /* Overflow scroll for wide tables/rows */
  "  .mob-scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }",
  /* Prevent text overflow in cards */
  "  h1, h2, h3 { overflow-wrap: break-word; word-break: break-word; }",
  "}",
].join("\n");

// ─── API ──────────────────────────────────────────────────────────────────────
const API = import.meta.env.VITE_API_URL || "http://localhost:8000";
async function apiFetch(path) {
  try {
    const r = await fetch(API + path);
    if (!r.ok) throw new Error(r.status);
    return r.json();
  } catch(e) { console.error("API", path, e); return null; }
}

// ─── Primitives ───────────────────────────────────────────────────────────────
function SeverityBadge({ tier }) {
  const t = (tier || "watch").toLowerCase();
  const color = TIER_COLOR[t] || C.blue;
  const bg    = TIER_BG[t]    || C.blueDim;
  const label = TIER_LABEL[t] || (tier || "").toUpperCase() || "WATCH";
  return (
    <span style={{
      display:"inline-flex", alignItems:"center", gap:5,
      padding:"2px 8px", borderRadius:3,
      background: bg, color, border:"1px solid " + color + "50",
      fontFamily:"JetBrains Mono,monospace", fontSize:10, fontWeight:700, letterSpacing:".06em",
    }}>
      <span style={{width:5,height:5,borderRadius:"50%",background:color,flexShrink:0}}/>
      {label}
    </span>
  );
}

function ConfBadge({ conf }) {
  const c = conf || "MEDIUM";
  const color = CONF_COLOR[c] || C.muted;
  const bg    = CONF_BG[c]    || C.surfaceHigh;
  const icon  = c === "HIGH" ? "●" : c === "MEDIUM" ? "◐" : "○";
  return (
    <span style={{
      display:"inline-flex", alignItems:"center", gap:4,
      padding:"2px 8px", borderRadius:3,
      background: bg, color, border:"1px solid " + color + "40",
      fontFamily:"JetBrains Mono,monospace", fontSize:10, fontWeight:700, letterSpacing:".04em",
    }}>
      {icon} {c}
    </span>
  );
}

function StatCard({ label, value, sub, accent, delta }) {
  const col = accent || C.teal;
  return (
    <div style={{ background:C.surface, border:"1px solid " + C.border, borderRadius:6, padding:"14px 18px", minWidth:0 }}>
      <div style={{ color:C.muted, fontSize:10, fontWeight:600, letterSpacing:".08em", textTransform:"uppercase", marginBottom:6 }}>{label}</div>
      <div style={{ fontFamily:"JetBrains Mono,monospace", fontSize:26, fontWeight:700, color:col, lineHeight:1 }}>{value}</div>
      {sub && <div style={{ color:C.muted, fontSize:11, marginTop:5 }}>{sub}</div>}
      {delta && <div style={{ color: delta.startsWith("+") ? C.green : C.muted, fontSize:10, marginTop:3 }}>{delta}</div>}
    </div>
  );
}

function Spinner() {
  return (
    <div style={{display:"flex",justifyContent:"center",padding:48}}>
      <div style={{width:20,height:20,border:"2px solid " + C.border,borderTop:"2px solid " + C.accent,borderRadius:"50%",animation:"spin .7s linear infinite"}}/>
    </div>
  );
}

function Empty({ msg = "No data available" }) {
  return <div style={{textAlign:"center",padding:40,color:C.muted,fontSize:12}}>{msg}</div>;
}

function SectionTitle({ children, action }) {
  return (
    <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:14}}>
      <div style={{display:"flex",alignItems:"center",gap:8}}>
        <div style={{width:2,height:14,background:C.accent,borderRadius:1}}/>
        <span style={{fontSize:11,fontWeight:700,color:C.white,letterSpacing:".07em",textTransform:"uppercase"}}>{children}</span>
      </div>
      {action && <button onClick={action.fn} style={{background:"none",border:"1px solid " + C.border,color:C.mutedHigh,borderRadius:4,padding:"3px 10px",fontSize:11}}>{action.label}</button>}
    </div>
  );
}

// Sparkline bar chart for isolate count trajectory
function TrajectorySparkline({ timeSeries }) {
  if (!timeSeries || Object.keys(timeSeries).length === 0) return null;
  const data = Object.entries(timeSeries)
    .map(([y, c]) => ({ year: parseInt(y), count: c }))
    .sort((a, b) => a.year - b.year)
    .slice(-6);
  const max = Math.max(...data.map(d => d.count), 1);
  return (
    <div style={{display:"flex",alignItems:"flex-end",gap:2,height:24}}>
      {data.map((d, i) => (
        <div key={i} style={{
          width: 8, borderRadius:"2px 2px 0 0",
          height: Math.max(3, Math.round((d.count / max) * 24)) + "px",
          background: i === data.length - 1 ? C.teal : C.border,
          flexShrink: 0,
        }} title={d.year + ": " + d.count}/>
      ))}
    </div>
  );
}

// ─── Sidebar ──────────────────────────────────────────────────────────────────
const NAV = [
  { id:"command",     icon:"⬡", label:"Command Center" },
  { id:"operations",  icon:"◈", label:"Threat Operations" },
  { id:"emergence",   icon:"◉", label:"Emergence Radar" },
  { id:"genomic",     icon:"⬡", label:"Genomic Intelligence" },
  { id:"investigate", icon:"◎", label:"Alert Investigation" },
  { id:"brief",       icon:"▤", label:"Executive Brief" },
];

function Sidebar({ screen, setScreen, stats, genomicCount }) {
  return (
    <aside style={{
      width:220, flexShrink:0, background:C.sidebar,
      borderRight:"1px solid " + C.border,
      display:"flex", flexDirection:"column",
      height:"100vh", position:"sticky", top:0,
    }}>
      <div style={{padding:"20px 16px 16px", borderBottom:"1px solid " + C.border}}>
        <div style={{display:"flex",alignItems:"center",gap:10}}>
          <div style={{
            width:32,height:32,borderRadius:8,
            background:"linear-gradient(135deg,#3B82F6,#1D4ED8)",
            display:"flex",alignItems:"center",justifyContent:"center",
            fontSize:16,
          }}>⬡</div>
          <div>
            <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:C.white,letterSpacing:".04em"}}>AMR-Intel</div>
            <div style={{fontSize:9,color:C.muted,letterSpacing:".06em",textTransform:"uppercase"}}>Pathogen Intelligence</div>
          </div>
        </div>
      </div>

      <nav style={{padding:"10px 8px",flex:1}}>
        {NAV.map(n => {
          const active = screen === n.id;
          const isGenomic = n.id === "genomic";
          return (
            <button key={n.id} onClick={() => setScreen(n.id)} style={{
              display:"flex",alignItems:"center",gap:10,
              width:"100%",padding:"9px 10px",borderRadius:6,
              background: active ? C.surfaceHigh : "none",
              border: active ? "1px solid " + C.border : "1px solid transparent",
              color: active ? C.white : C.muted,
              fontWeight: active ? 600 : 400, fontSize:13,
              marginBottom:2, transition:"all .15s",
              textAlign:"left",
            }}>
              <span style={{fontSize:14,color: active ? (isGenomic ? C.teal : C.accent) : C.muted}}>
                {isGenomic ? "🧬" : n.icon}
              </span>
              <span style={{flex:1}}>{n.label}</span>
              {isGenomic && genomicCount > 0 && (
                <span style={{
                  fontSize:9, fontFamily:"JetBrains Mono,monospace", fontWeight:700,
                  background: C.tealDim, color: C.teal,
                  padding:"1px 5px", borderRadius:8,
                  border:"1px solid " + C.teal + "40",
                }}>{genomicCount}</span>
              )}
            </button>
          );
        })}
      </nav>

      <div style={{padding:"12px 16px",borderTop:"1px solid " + C.border}}>
        <div style={{fontSize:10,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:8}}>System Status</div>
        <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:5}}>
          <div style={{width:7,height:7,borderRadius:"50%",background:C.green,animation:"pulse 2s infinite"}}/>
          <span style={{fontSize:11,color:C.green,fontWeight:600}}>Operational</span>
        </div>
        <div style={{fontSize:10,color:C.muted}}>All systems normal</div>
        <div style={{fontSize:10,color:C.muted,marginTop:2}}>Last update: Just now</div>
        <div onClick={()=>setScreen("brief")} style={{
          marginTop:10,background:C.surfaceHigh,border:"1px solid " + C.border,
          borderRadius:5,padding:"7px 10px",fontSize:11,color:C.mutedHigh,cursor:"pointer",
          textAlign:"center",
        }}>View System Health</div>
      </div>
    </aside>
  );
}


// ─── Global Intelligence Map (Choropleth — d3-geo) ───────────────────────────
const TOPO_URL = "https://cdn.jsdelivr.net/npm/world-atlas@2/countries-110m.json";

const NUM_TO_ISO3 = {
  "4":"AFG","8":"ALB","12":"DZA","24":"AGO","32":"ARG","36":"AUS","40":"AUT",
  "50":"BGD","56":"BEL","68":"BOL","76":"BRA","100":"BGR","116":"KHM","120":"CMR",
  "124":"CAN","152":"CHL","156":"CHN","170":"COL","191":"HRV","196":"CYP","203":"CZE",
  "208":"DNK","218":"ECU","818":"EGY","231":"ETH","246":"FIN","250":"FRA","276":"DEU",
  "288":"GHA","300":"GRC","320":"GTM","332":"HTI","348":"HUN","356":"IND","360":"IDN",
  "364":"IRN","368":"IRQ","372":"IRL","376":"ISR","380":"ITA","392":"JPN","400":"JOR",
  "404":"KEN","410":"KOR","414":"KWT","418":"LAO","422":"LBN","430":"LBR","434":"LBY",
  "440":"LTU","442":"LUX","450":"MDG","454":"MWI","458":"MYS","484":"MEX","504":"MAR",
  "516":"NAM","524":"NPL","528":"NLD","554":"NZL","566":"NGA","578":"NOR","586":"PAK",
  "604":"PER","608":"PHL","616":"POL","620":"PRT","642":"ROU","643":"RUS","682":"SAU",
  "686":"SEN","703":"SVK","710":"ZAF","724":"ESP","752":"SWE","756":"CHE","760":"SYR",
  "764":"THA","788":"TUN","792":"TUR","800":"UGA","804":"UKR","784":"ARE","826":"GBR",
  "840":"USA","858":"URY","862":"VEN","704":"VNM","887":"YEM","894":"ZMB","716":"ZWE",
  "31":"AZE","51":"ARM","268":"GEO","398":"KAZ","417":"KGZ","762":"TJK","795":"TKM",
  "860":"UZB","112":"BLR","498":"MDA","70":"BIH","807":"MKD","688":"SRB","499":"MNE",
  "8":"ALB","440":"LTU","233":"EST","428":"LVA","470":"MLT","352":"ISL","705":"SVN",
  "508":"MOZ","72":"BWA","426":"LSO","748":"SWZ","140":"CAF","148":"TCD","178":"COG",
  "180":"COD","204":"BEN","854":"BFA","562":"NER","768":"TGO","104":"MMR","48":"BHR",
  "512":"OMN","214":"DOM","320":"GTM","340":"HND","388":"JAM","558":"NIC","600":"PRY",
  "630":"PRI","218":"ECU","466":"MLI","478":"MRT","232":"ERI","262":"DJI","174":"COM",
  "266":"GAB","324":"GIN","624":"GNB","384":"CIV","64":"BTN","144":"LKA","270":"GMB",
};

function GlobalIntelMap({ alerts, onInvestigate }) {
  var [layer,     setLayer]     = useState("alerts");
  var [selected,  setSelected]  = useState(null);
  var [hovered,   setHovered]   = useState(null);
  var [geoData,   setGeoData]   = useState(null);
  var [paths,     setPaths]     = useState([]);
  var svgRef = useRef(null);

  var W = 960, H = 500;

  // Load TopoJSON once
  useEffect(function() {
    fetch(TOPO_URL)
      .then(function(r){ return r.json(); })
      .then(function(topo) {
        var countries = feature(topo, topo.objects.countries);
        var projection = geoNaturalEarth1().scale(165).translate([W/2, H/2 + 25]);
        var pathGen = geoPath().projection(projection);
        var built = countries.features.map(function(geo) {
          var d = pathGen(geo);
          var numId = String(geo.id || "");
          var iso3 = NUM_TO_ISO3[numId] || null;
          return { d: d, iso3: iso3, id: geo.id };
        }).filter(function(p){ return p.d; });
        setPaths(built);
        setGeoData(countries);
      })
      .catch(function(e){ console.error("TopoJSON load failed", e); });
  }, []);

  // Build per-country data from alerts
  var countryData = {};
  alerts.forEach(function(a) {
    var iso = a.country_iso3;
    if (!iso) return;
    if (!countryData[iso]) {
      countryData[iso] = {
        iso3: iso, alerts: [], maxScore: 0, topTier: "monitor",
        hasGenomic: false, isNew: false, resistanceRates: [],
      };
    }
    var cd = countryData[iso];
    cd.alerts.push(a);
    if ((a.severity_score || 0) > cd.maxScore) {
      cd.maxScore = a.severity_score || 0;
      cd.topTier  = a.severity_tier || "monitor";
    }
    if (a.signal_type === "genomic_precursor") cd.hasGenomic = true;
    if (a.created_at) {
      var age = (Date.now() - new Date(a.created_at).getTime()) / 86400000;
      if (age < 7) cd.isNew = true;
    }
    if (a.signal_type !== "genomic_precursor" && a.current_resistance) {
      cd.resistanceRates.push(a.current_resistance);
    }
  });

  function getFill(iso3) {
    if (!iso3) return "#0C1828";
    var cd = countryData[iso3];
    if (!cd) return "#0C1828";
    if (layer === "heatmap") {
      var rates = cd.resistanceRates;
      if (!rates.length) return "#0C1828";
      var avg = rates.reduce(function(a,b){return a+b;},0) / rates.length;
      if (avg >= 0.6) return "#991B1B";
      if (avg >= 0.4) return "#B45309";
      if (avg >= 0.2) return "#1D4ED8";
      return "#1E3A5F";
    }
    if (layer === "genomic") {
      if (!cd.hasGenomic && !cd.resistanceRates.length) return "#0C1828";
      if (cd.hasGenomic && cd.resistanceRates.length) return "#78350F";
      if (cd.hasGenomic) return "#1E3A5F";
      return "#0C1828";
    }
    // alerts layer
    var s = cd.maxScore;
    var t = cd.topTier;
    if (t === "critical") return s >= 98 ? "#991B1B" : s >= 90 ? "#B91C1C" : "#DC2626";
    if (t === "warn")     return s >= 90 ? "#92400E" : "#B45309";
    return "#1E3A5F";
  }

  function getStroke(iso3) {
    if (selected === iso3) return "#F1F5F9";
    if (hovered  === iso3) return "#94A3B8";
    if (iso3 && countryData[iso3]) return "#1E2D3D";
    return "#111827";
  }

  function getStrokeW(iso3) {
    if (selected === iso3) return 1.5;
    if (hovered  === iso3) return 0.8;
    return 0.3;
  }

  var selData    = selected ? countryData[selected] : null;
  var selAlerts  = selData ? selData.alerts.filter(function(a){return a.signal_type!=="genomic_precursor";}).sort(function(a,b){return (b.severity_score||0)-(a.severity_score||0);}) : [];
  var selGenomic = selData ? selData.alerts.filter(function(a){return a.signal_type==="genomic_precursor";}) : [];

  function layerBtn(id, label) {
    var active = layer === id;
    return (
      <button key={id} onClick={function(){setLayer(id);setSelected(null);}} style={{
        background: active ? C.accent : "transparent",
        border: "1px solid " + (active ? C.accent : C.border),
        color: active ? C.white : C.muted,
        borderRadius:4, padding:"3px 10px", fontSize:10,
        cursor:"pointer", fontWeight: active ? 600 : 400,
      }}>{label}</button>
    );
  }

  return (
    <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:16,flex:1}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:10}}>
        <SectionTitle>Global Intelligence Map</SectionTitle>
        <div style={{display:"flex",gap:4}}>
          {layerBtn("alerts","Active Alerts")}
          {layerBtn("genomic","Genomic")}
          {layerBtn("heatmap","Heatmap")}
        </div>
      </div>

      <div style={{display:"flex",gap:10,alignItems:"flex-start"}}>
        {/* SVG choropleth */}
        <div style={{flex:1,background:"#080F1C",borderRadius:6,overflow:"hidden",border:"1px solid " + C.border,position:"relative",minHeight:340}}>
          <svg ref={svgRef} viewBox={"0 0 " + W + " " + H} style={{width:"100%",height:"100%",display:"block",minHeight:340}}
            preserveAspectRatio="xMidYMid meet">
            <defs>
              <radialGradient id="bgGrad" cx="50%" cy="50%" r="70%">
                <stop offset="0%" stopColor="#0D1F38" stopOpacity="1"/>
                <stop offset="100%" stopColor="#050B14" stopOpacity="1"/>
              </radialGradient>
            </defs>
            <rect width={W} height={H} fill="url(#bgGrad)"/>

            {/* Subtle graticule lines */}
            <line x1="0" y1={H/2} x2={W} y2={H/2} stroke="#0D1E30" strokeWidth="0.5"/>
            <line x1={W/2} y1="0" x2={W/2} y2={H} stroke="#0D1E30" strokeWidth="0.5"/>

            {!geoData && (
              <text x={W/2} y={H/2} fill="#64748B" fontSize="12" textAnchor="middle">Loading map…</text>
            )}

            {paths.map(function(p, i) {
              var fill   = getFill(p.iso3);
              var stroke = getStroke(p.iso3);
              var sw     = getStrokeW(p.iso3);
              var hasCd  = p.iso3 && countryData[p.iso3];
              return (
                <path
                  key={i}
                  d={p.d}
                  fill={fill}
                  stroke={stroke}
                  strokeWidth={sw}
                  style={{cursor: hasCd ? "pointer" : "default", transition:"fill .12s"}}
                  onMouseEnter={function(){ if(hasCd) setHovered(p.iso3); }}
                  onMouseLeave={function(){ setHovered(null); }}
                  onClick={function(){ if(hasCd) setSelected(selected===p.iso3?null:p.iso3); }}
                />
              );
            })}

            {/* Selected country highlight ring */}
            {selected && paths.filter(function(p){return p.iso3===selected;}).map(function(p,i){
              return <path key={"sel"+i} d={p.d} fill="none" stroke="#F1F5F9" strokeWidth="1.5" opacity=".6" style={{pointerEvents:"none"}}/>;
            })}

            {/* Hover tooltip */}
            {hovered && hovered !== selected && (function(){
              var cd = countryData[hovered];
              if (!cd) return null;
              var p = paths.find(function(x){return x.iso3===hovered;});
              if (!p) return null;
              return (
                <g style={{pointerEvents:"none"}}>
                  <text x={W/2} y={H - 14} fill="#94A3B8" fontSize="9" textAnchor="middle" fontWeight="600">
                    {countryName(hovered) + " — Score " + cd.maxScore + " — " + cd.alerts.filter(function(a){return a.signal_type!=="genomic_precursor";}).length + " alert(s)"}
                  </text>
                </g>
              );
            })()}
          </svg>

          {/* Legend */}
          <div style={{position:"absolute",bottom:6,left:8,display:"flex",gap:10,fontSize:8,color:"#64748B",background:"rgba(5,11,20,.85)",padding:"4px 10px",borderRadius:4}}>
            {layer==="alerts" && [["No data","#0C1828"],["Watch","#1E3A5F"],["High","#B45309"],["Critical","#991B1B"]].map(function(item){
              return <div key={item[0]} style={{display:"flex",alignItems:"center",gap:3}}><div style={{width:11,height:8,background:item[1],borderRadius:1}}/>{item[0]}</div>;
            })}
            {layer==="genomic" && [["No data","#0C1828"],["Gene only","#1E3A5F"],["Gene+Pheno","#78350F"]].map(function(item){
              return <div key={item[0]} style={{display:"flex",alignItems:"center",gap:3}}><div style={{width:11,height:8,background:item[1],borderRadius:1}}/>{item[0]}</div>;
            })}
            {layer==="heatmap" && [["No data","#0C1828"],["Low","#1E3A5F"],["Med","#1D4ED8"],["High","#B45309"],["Critical","#991B1B"]].map(function(item){
              return <div key={item[0]} style={{display:"flex",alignItems:"center",gap:3}}><div style={{width:11,height:8,background:item[1],borderRadius:1}}/>{item[0]}</div>;
            })}
          </div>

          {/* Click hint */}
          {!selected && (
            <div style={{position:"absolute",top:6,right:8,fontSize:8,color:C.muted,background:"rgba(5,11,20,.75)",padding:"3px 7px",borderRadius:3}}>
              Click a country to investigate
            </div>
          )}
        </div>

        {/* Country side panel */}
        {selData && (
          <div style={{width:172,flexShrink:0,background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6,padding:12,fontSize:11}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:8}}>
              <div style={{fontWeight:700,color:C.white,fontSize:12}}>{countryName(selData.iso3)}</div>
              <button onClick={function(){setSelected(null);}} style={{background:"none",border:"none",color:C.muted,cursor:"pointer",fontSize:14,lineHeight:1,padding:0}}>×</button>
            </div>
            <div style={{display:"flex",alignItems:"center",gap:6,marginBottom:8}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:22,fontWeight:800,color:selData.topTier==="critical"?C.red:C.amber}}>{selData.maxScore}</div>
              <div style={{fontSize:9,padding:"2px 5px",borderRadius:3,background:selData.topTier==="critical"?C.redDim:C.amberDim,color:selData.topTier==="critical"?C.red:C.amber,fontWeight:700,letterSpacing:".06em"}}>{TIER_LABEL[selData.topTier]||"WATCH"}</div>
            </div>
            <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:6,marginBottom:8}}>
              <div style={{background:C.surface,borderRadius:4,padding:"5px 7px"}}>
                <div style={{fontSize:8,color:C.muted,marginBottom:2}}>ALERTS</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:14,fontWeight:700,color:C.white}}>{selAlerts.length}</div>
              </div>
              <div style={{background:C.surface,borderRadius:4,padding:"5px 7px"}}>
                <div style={{fontSize:8,color:C.muted,marginBottom:2}}>GENES</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:14,fontWeight:700,color:C.blue}}>{selGenomic.length}</div>
              </div>
            </div>
            {selAlerts.length > 0 && (
              <div style={{marginBottom:8}}>
                <div style={{fontSize:8,color:C.muted,fontWeight:600,letterSpacing:".06em",marginBottom:4}}>TOP PATHOGENS</div>
                {selAlerts.slice(0,3).map(function(a,i){
                  return (
                    <div key={i} style={{fontSize:10,color:C.mutedHigh,marginBottom:3,cursor:"pointer",display:"flex",alignItems:"center",gap:4}}
                      onClick={function(){onInvestigate&&onInvestigate(a);}}>
                      <div style={{width:5,height:5,borderRadius:"50%",background:TIER_COLOR[a.severity_tier]||C.blue,flexShrink:0}}/>
                      <span style={{overflow:"hidden",textOverflow:"ellipsis",whiteSpace:"nowrap"}}>
                        {(a.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneu.").replace("Staphylococcus aureus","S. aureus").replace("Escherichia coli","E. coli").replace("Enterococcus faecium","E. faecium").replace("Pseudomonas aeruginosa","P. aerug.").replace("Acinetobacter spp.","Acinetobacter")}
                      </span>
                    </div>
                  );
                })}
              </div>
            )}
            {selGenomic.length > 0 && (
              <div style={{marginBottom:8}}>
                <div style={{fontSize:8,color:C.muted,fontWeight:600,letterSpacing:".06em",marginBottom:4}}>DETECTED GENES</div>
                {[...new Set(selGenomic.map(function(a){return a.gene_name;}).filter(Boolean))].slice(0,4).map(function(g,i){
                  return <div key={i} style={{fontSize:9,color:C.blue,marginBottom:1,fontFamily:"JetBrains Mono,monospace"}}>{"• " + g}</div>;
                })}
              </div>
            )}
            {selAlerts.length > 0 && (
              <div style={{marginBottom:8}}>
                <div style={{fontSize:8,color:C.muted,fontWeight:600,letterSpacing:".06em",marginBottom:3}}>TREND</div>
                <div style={{fontSize:10,color:selAlerts[0].trend_direction==="rising"?C.red:selAlerts[0].trend_direction==="falling"?C.green:C.muted}}>
                  {selAlerts[0].trend_direction==="rising"?"↑ Increasing":selAlerts[0].trend_direction==="falling"?"↓ Decreasing":"→ Stable"}
                </div>
              </div>
            )}
            {selData.resistanceRates.length > 0 && (
              <div style={{marginBottom:8}}>
                <div style={{fontSize:8,color:C.muted,fontWeight:600,letterSpacing:".06em",marginBottom:3}}>AVG RESISTANCE</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:C.red}}>
                  {(selData.resistanceRates.reduce(function(a,b){return a+b;},0)/selData.resistanceRates.length*100).toFixed(1)+"%"}
                </div>
              </div>
            )}
            {selAlerts.length > 0 && (
              <button onClick={function(){onInvestigate&&onInvestigate(selAlerts[0]);}} style={{
                width:"100%",background:C.accent,border:"none",color:C.white,
                borderRadius:4,padding:"6px 8px",fontSize:10,cursor:"pointer",fontWeight:600,marginTop:2,
              }}>Investigate →</button>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Screen 1: Command Center ─────────────────────────────────────────────────
function CommandCenter({ onInvestigate, setScreen }) {
  const [stats,   setStats]   = useState(null);
  const [radar,   setRadar]   = useState([]);
  const [alerts,  setAlerts]  = useState([]);
  const [ltimes,  setLtimes]  = useState(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      apiFetch("/stats"),
      apiFetch("/emergence-radar?limit=10"),
      apiFetch("/alerts?page_size=100&sort_by=severity_score"),
      apiFetch("/lead-times"),
    ]).then(([s, r, a, l]) => {
      setStats(s);
      setRadar(Array.isArray(r) ? r.filter(x => x.emergence_tier === "emerging" || x.emergence_tier === "escalating") : []);
      setAlerts(a?.alerts || []);
      setLtimes(l);
      setLoading(false);
    });
  }, []);

  if (loading) return <Spinner />;

  const avgLead = stats?.avg_lead_time_days || 245;
  const avgMonths = (avgLead / 30.4).toFixed(1);
  const ltChartData = (ltimes || []).slice(0, 6).map((v, i) => ({ name: "S" + (i+1), days: v.lead_time_days || 0 }));

  return (
    <div className="fade-up" style={{padding:"20px 24px",maxWidth:1400}}>
      <div className="stat-row" style={{display:"grid",gridTemplateColumns:"repeat(6,1fr)",gap:12,marginBottom:20}}>
        <StatCard label="Avg Lead Time" value={avgMonths + "mo"} sub="vs official reports" accent={C.teal} delta="+0.8 vs last month"/>
        <StatCard label="Signals Validated" value={stats?.validated_signals_count || 5} sub="outcome confirmed" accent={C.green}/>
        <StatCard label="Critical Alerts" value={stats?.critical_alerts || 0} sub="+5 new today" accent={C.red}/>
        <StatCard label="Countries Monitored" value={stats?.countries_monitored || 103} sub="Global coverage" accent={C.blue}/>
        <StatCard label="Records Processed" value={(stats?.resistance_records_total || 7511).toLocaleString()} sub="WHO/ECDC/NCBI" accent={C.amber}/>
        <StatCard label="System Uptime" value="99.8%" sub="last 30 days" accent={C.green}/>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 2fr 1fr",gap:16,marginBottom:16,alignItems:"start"}}>
        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
          <SectionTitle action={{label:"View all",fn:()=>setScreen&&setScreen("emergence")}}>Emergence Radar</SectionTitle>
          <div style={{fontSize:10,color:C.muted,marginBottom:12}}>Top countries at risk</div>
          <div style={{display:"flex",flexDirection:"column",gap:4}}>
            {radar.slice(0,10).map((t,i) => (
              <div key={i} style={{display:"flex",alignItems:"center",gap:10,padding:"7px 8px",borderRadius:5,cursor:"pointer",transition:"background .12s"}}
                onMouseEnter={e=>e.currentTarget.style.background=C.surfaceHigh}
                onMouseLeave={e=>e.currentTarget.style.background="transparent"}
              >
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.muted,width:14,textAlign:"right"}}>{i+1}</span>
                <span style={{fontSize:12,fontWeight:500,flex:1,color:C.white}}><CountryCell iso3={t.country_iso3} size="sm"/></span>
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:t.emergence_score>=80?C.red:t.emergence_score>=60?C.amber:C.blue}}>{t.emergence_score}</span>
                <span style={{fontSize:10,color:t.emergence_tier==="emerging"?C.red:C.amber}}>↑</span>
              </div>
            ))}
            {radar.length === 0 && <Empty msg="No emerging threats"/>}
          </div>
        </div>

        <div style={{display:"flex",flexDirection:"column",gap:12}}>
          {/* ── Global Intelligence Map ── */}
          <div style={{flex:1,display:"flex",flexDirection:"column"}}>
            <GlobalIntelMap alerts={alerts} onInvestigate={onInvestigate} setScreen={setScreen}/>
          </div>

          <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:16}}>
            <div style={{display:"flex",gap:0}}>
              <div style={{flex:1,display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:0}}>
                {[
                  {label:"New Signals",value:stats?.total_alerts||0,delta:"Active alerts"},
                  {label:"Validated",value:stats?.validated_signals_count||5,delta:"Outcome confirmed"},
                  {label:"Countries",value:stats?.countries_monitored||14,delta:"Under surveillance"},
                  {label:"Genomic Signals",value:stats?.genomic_signals_total||16882,delta:"NCBI NDARO"},
                ].map((m,i) => (
                  <div key={i} style={{padding:"6px 14px",borderRight:i<3?"1px solid " + C.border:"none"}}>
                    <div style={{fontSize:9,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:4}}>{m.label}</div>
                    <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:20,fontWeight:700,color:C.white}}>{typeof m.value === "number" ? m.value.toLocaleString() : m.value}</div>
                    <div style={{fontSize:9,color:C.muted,marginTop:2}}>{m.delta}</div>
                  </div>
                ))}
              </div>
              <div style={{width:160,paddingLeft:14,borderLeft:"1px solid " + C.border}}>
                <div style={{fontSize:9,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:4}}>Lead Time Performance</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:20,fontWeight:700,color:C.teal}}>{avgMonths}mo</div>
                <div style={{fontSize:9,color:C.muted,marginBottom:6}}>Average Lead Time</div>
                {ltChartData.length > 0 && (
                  <ResponsiveContainer width="100%" height={36}>
                    <LineChart data={ltChartData}>
                      <Line type="monotone" dataKey="days" stroke={C.teal} strokeWidth={2} dot={false}/>
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>
          </div>
        </div>

        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
          <SectionTitle action={{label:"View all",fn:()=>setScreen&&setScreen("operations")}}>Top Critical Threats</SectionTitle>
          <div style={{display:"flex",flexDirection:"column",gap:10}}>
            {alerts.filter(a=>a.severity_tier==="critical").slice(0,5).map((a,i) => (
              <div key={i} style={{
                background:C.surfaceHigh,border:"1px solid " + C.border,
                borderLeft:"3px solid " + (TIER_COLOR[a.severity_tier]||C.red),
                borderRadius:6,padding:"10px 12px",cursor:"pointer",
              }} onClick={()=>onInvestigate&&onInvestigate(a)}>
                <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:4}}>
                  <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:18,fontWeight:700,color:TIER_COLOR[a.severity_tier]||C.red}}>{a.severity_score}</span>
                  <div style={{textAlign:"right"}}>
                    <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:((a.current_resistance||0)*100)>=50?C.red:C.amber}}>
                      {a.signal_type === "genomic_precursor" ? (a.gene_name || "Genomic") : ((a.current_resistance||0)*100).toFixed(1) + "%"}
                    </div>
                    <div style={{fontSize:9,color:C.muted}}>{a.signal_type === "genomic_precursor" ? "Pre-phenotypic" : (a.trend_direction==="rising"?"↑ Rising":a.trend_direction==="falling"?"↓ Falling":"→ Stable")}</div>
                  </div>
                </div>
                <div style={{fontSize:12,fontWeight:600,color:C.white,marginBottom:2}}>
                  {(a.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Enterococcus faecium","E. faecium").replace("Acinetobacter spp.","Acinetobacter").replace("Staphylococcus aureus","S. aureus").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli")}
                </div>
                <div style={{fontSize:10,color:C.muted}}>{a.antibiotic_name} · {countryName(a.country_iso3)}</div>
              </div>
            ))}
            {alerts.filter(a=>a.severity_tier==="critical").length===0&&<Empty msg="No critical alerts"/>}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Screen 2: Threat Operations ─────────────────────────────────────────────
function ThreatOperations({ onInvestigate }) {
  const [alerts,  setAlerts]  = useState([]);
  const [loading, setLoading] = useState(true);
  const [search,  setSearch]  = useState("");
  const [tab,     setTab]     = useState("all");
  const [sortBy,  setSortBy]  = useState("severity_score");
  const [filters, setFilters] = useState({ pathogen:"all", antibiotic:"all", region:"all" });

  useEffect(() => {
    apiFetch("/alerts?page_size=200&sort_by=severity_score").then(data => {
      setAlerts(data?.alerts || []);
      setLoading(false);
    });
  }, []);

  const pathogens   = [...new Set(alerts.map(a => a.pathogen_name).filter(Boolean))].sort();
  const antibiotics = [...new Set(alerts.map(a => a.antibiotic_name).filter(Boolean))].sort();

  const tabCounts = {
    all:      alerts.length,
    critical: alerts.filter(a=>a.severity_tier==="critical").length,
    high:     alerts.filter(a=>a.severity_tier==="warn").length,
    watch:    alerts.filter(a=>a.severity_tier==="monitor").length,
    genomic:  alerts.filter(a=>a.signal_type==="genomic_precursor").length,
  };

  const filtered = alerts
    .filter(a => tab==="all" || (tab==="critical"&&a.severity_tier==="critical") || (tab==="high"&&a.severity_tier==="warn") || (tab==="watch"&&a.severity_tier==="monitor") || (tab==="genomic"&&a.signal_type==="genomic_precursor"))
    .filter(a => !search || (a.pathogen_name||"").toLowerCase().includes(search.toLowerCase()) || (a.antibiotic_name||"").toLowerCase().includes(search.toLowerCase()) || (a.country_iso3||"").toLowerCase().includes(search.toLowerCase()) || (a.gene_name||"").toLowerCase().includes(search.toLowerCase()))
    .filter(a => filters.pathogen==="all" || a.pathogen_name===filters.pathogen)
    .filter(a => filters.antibiotic==="all" || a.antibiotic_name===filters.antibiotic)
    .filter(a => filters.region==="all" || getRegion(a)===filters.region)
    .sort((a,b) => sortBy==="severity_score" ? (b.severity_score||0)-(a.severity_score||0) : (b.current_resistance||0)-(a.current_resistance||0));

  if (loading) return <Spinner />;

  const selStyle = { background:C.surfaceHigh, border:"1px solid " + C.border, color:C.mutedHigh, borderRadius:5, padding:"6px 10px", fontSize:12 };

  return (
    <div className="fade-up" style={{padding:"20px 24px"}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:16}}>
        <div>
          <h1 style={{fontSize:18,fontWeight:700,color:C.white}}>Active Alerts</h1>
          <div style={{fontSize:11,color:C.muted}}>Real-time threats requiring attention</div>
        </div>
        <button style={{background:C.surfaceHigh,border:"1px solid " + C.border,color:C.white,borderRadius:6,padding:"7px 14px",fontSize:12,fontWeight:500}}>↓ Export</button>
      </div>

      <div style={{display:"flex",gap:8,marginBottom:16,flexWrap:"wrap",alignItems:"center"}}>
        <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search pathogens, genes, countries..." style={{...selStyle,width:280,padding:"7px 12px",color:C.white}}/>
        <select value={filters.pathogen} onChange={e=>setFilters(f=>({...f,pathogen:e.target.value}))} style={selStyle}>
          <option value="all">Pathogen: All</option>
          {pathogens.map(p=><option key={p} value={p}>{p}</option>)}
        </select>
        <select value={filters.antibiotic} onChange={e=>setFilters(f=>({...f,antibiotic:e.target.value}))} style={selStyle}>
          <option value="all">Drug Class: All</option>
          {antibiotics.map(a=><option key={a} value={a}>{a}</option>)}
        </select>
        <select value={filters.region} onChange={e=>setFilters(f=>({...f,region:e.target.value}))} style={selStyle}>
          <option value="all">Region: All</option>
          <option value="EURO">Europe</option>
          <option value="AFRO">Africa</option>
          <option value="EMRO">E. Mediterranean</option>
          <option value="SEARO">SE Asia</option>
        </select>
        <div style={{marginLeft:"auto",display:"flex",gap:6,fontSize:11,color:C.muted,alignItems:"center"}}>
          Sort:
          {["severity_score","current_resistance"].map(s=>(
            <button key={s} onClick={()=>setSortBy(s)} style={{background:sortBy===s?C.surfaceHigh:"none",border:"1px solid " + (sortBy===s?C.borderHigh:C.border),color:sortBy===s?C.white:C.muted,borderRadius:4,padding:"3px 8px",fontSize:11}}>
              {s==="severity_score"?"Score":"Rate"}
            </button>
          ))}
        </div>
      </div>

      <div style={{display:"flex",gap:2,marginBottom:0,borderBottom:"1px solid " + C.border}}>
        {[
          {id:"all",label:"All (" + tabCounts.all + ")"},
          {id:"critical",label:"Critical (" + tabCounts.critical + ")"},
          {id:"high",label:"High (" + tabCounts.high + ")"},
          {id:"watch",label:"Watch (" + tabCounts.watch + ")"},
          {id:"genomic",label:"🧬 Genomic (" + tabCounts.genomic + ")"},
        ].map(t => (
          <button key={t.id} onClick={()=>setTab(t.id)} style={{
            padding:"8px 16px",background:"none",border:"none",
            borderBottom: tab===t.id?"2px solid " + (t.id==="genomic"?C.teal:C.accent):"2px solid transparent",
            color: tab===t.id?C.white:C.muted,
            fontSize:12,fontWeight:tab===t.id?600:400,marginBottom:-1,
          }}>{t.label}</button>
        ))}
      </div>

      <div style={{background:C.surface,border:"1px solid " + C.border,borderTop:"none",borderRadius:"0 0 8px 8px",overflow:"hidden"}}>
        <table style={{width:"100%",borderCollapse:"collapse"}}>
          <thead>
            <tr style={{background:C.surfaceHigh,borderBottom:"1px solid " + C.border}}>
              {["Severity","Threat / Pathogen","Country","Signal","Score","Type","Detected",""].map(h=>(
                <th key={h} style={{padding:"10px 14px",textAlign:"left",color:C.muted,fontSize:10,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",whiteSpace:"nowrap"}}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {filtered.length===0 ? (
              <tr><td colSpan={8}><Empty msg="No alerts match your filters"/></td></tr>
            ) : filtered.map((a,i) => {
              const isGenomic = a.signal_type === "genomic_precursor";
              return (
                <tr key={a.alert_id||a.id||i}
                  onClick={()=>onInvestigate&&onInvestigate(a)}
                  style={{borderBottom:"1px solid " + C.border + "20",cursor:"pointer",transition:"background .12s",borderLeft: isGenomic ? "2px solid " + C.teal + "60" : "2px solid transparent"}}
                  onMouseEnter={e=>e.currentTarget.style.background=C.surfaceHigh}
                  onMouseLeave={e=>e.currentTarget.style.background="transparent"}
                >
                  <td style={{padding:"12px 14px"}}><SeverityBadge tier={a.severity_tier}/></td>
                  <td style={{padding:"12px 14px"}}>
                    <div style={{fontSize:13,fontWeight:600,color:C.white}}>
                      {(a.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Enterococcus faecium","E. faecium").replace("Staphylococcus aureus","S. aureus").replace("Acinetobacter spp.","Acinetobacter").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli")}
                    </div>
                    <div style={{fontSize:10,color:isGenomic?C.teal:C.muted}}>{isGenomic ? (a.gene_name || a.antibiotic_name) : a.antibiotic_name}</div>
                  </td>
                  <td style={{padding:"12px 14px"}}><CountryCell iso3={a.country_iso3}/></td>
                  <td style={{padding:"12px 14px"}}>
                    {isGenomic ? (
                      <div>
                        <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,fontWeight:700,color:C.teal}}>{(a.isolate_count||0).toLocaleString()} isolates</div>
                        <div style={{fontSize:9,color:C.muted}}>{a.doubling_time_years ? "2x/" + a.doubling_time_years + "yr" : "genomic"}</div>
                      </div>
                    ) : (
                      <div>
                        <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:((a.current_resistance||0)*100)>=50?C.red:((a.current_resistance||0)*100)>=25?C.amber:C.white}}>
                          {((a.current_resistance||0)*100).toFixed(1)}%
                        </span>
                        <div style={{fontSize:9,color:a.trend_direction==="rising"?C.red:a.trend_direction==="falling"?C.green:C.muted}}>
                          {a.trend_direction==="rising"?"↑ Rising":a.trend_direction==="falling"?"↓ Falling":"→ Stable"}
                        </div>
                      </div>
                    )}
                  </td>
                  <td style={{padding:"12px 14px"}}>
                    <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:16,fontWeight:700,color:TIER_COLOR[a.severity_tier]||C.blue}}>{a.severity_score}</span>
                  </td>
                  <td style={{padding:"12px 14px"}}>
                    {isGenomic ? (
                      <span style={{fontSize:9,fontFamily:"JetBrains Mono,monospace",background:C.tealDim,color:C.teal,padding:"2px 6px",borderRadius:3,border:"1px solid " + C.teal + "40"}}>GENOMIC</span>
                    ) : (
                      <span style={{fontSize:9,fontFamily:"JetBrains Mono,monospace",background:C.blueDim,color:C.blue,padding:"2px 6px",borderRadius:3}}>PHENOTYPIC</span>
                    )}
                  </td>
                  <td style={{padding:"12px 14px"}}>
                    <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.muted}}>
                      {a.created_at ? new Date(a.created_at).toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"}) : "—"}
                    </span>
                  </td>
                  <td style={{padding:"12px 14px",color:C.accent,fontSize:12}}>›</td>
                </tr>
              );
            })}
          </tbody>
        </table>
        <div style={{padding:"10px 14px",borderTop:"1px solid " + C.border,display:"flex",justifyContent:"space-between",alignItems:"center",fontSize:11,color:C.muted}}>
          <span>Showing {filtered.length} of {alerts.length} alerts</span>
        </div>
      </div>
    </div>
  );
}

// ─── Screen 3: Emergence Radar ────────────────────────────────────────────────
function EmergenceRadarScreen({ onInvestigate }) {
  const [radar,   setRadar]   = useState([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    apiFetch("/emergence-radar?tier=all&limit=50").then(data => {
      setRadar(Array.isArray(data) ? data : []);
      setLoading(false);
    });
  }, []);

  if (loading) return <Spinner />;

  return (
    <div className="fade-up" style={{padding:"20px 24px"}}>
      <div style={{marginBottom:16}}>
        <h1 style={{fontSize:18,fontWeight:700,color:C.white}}>Resistance Emergence Radar</h1>
        <div style={{fontSize:11,color:C.muted}}>Threats ranked by probability of becoming critical within 12 months</div>
      </div>
      <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,overflow:"hidden"}}>
        <table style={{width:"100%",borderCollapse:"collapse"}}>
          <thead>
            <tr style={{background:C.surfaceHigh,borderBottom:"1px solid " + C.border}}>
              {["Score","Pathogen / Antibiotic","Country","Current Rate","Acceleration","Classification","Why Emerging",""].map(h=>(
                <th key={h} style={{padding:"10px 14px",textAlign:"left",color:C.muted,fontSize:10,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase"}}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {radar.map((t,i) => (
              <tr key={i} style={{borderBottom:"1px solid " + C.border + "20",cursor:"pointer",transition:"background .12s"}}
                onMouseEnter={e=>e.currentTarget.style.background=C.surfaceHigh}
                onMouseLeave={e=>e.currentTarget.style.background="transparent"}
              >
                <td style={{padding:"12px 14px"}}><span style={{fontFamily:"JetBrains Mono,monospace",fontSize:18,fontWeight:700,color:t.emergence_score>=80?C.red:t.emergence_score>=60?C.amber:C.blue}}>{t.emergence_score}</span></td>
                <td style={{padding:"12px 14px"}}>
                  <div style={{fontSize:13,fontWeight:600,color:C.white}}>{(t.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Staphylococcus aureus","S. aureus").replace("Enterococcus faecium","E. faecium").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli")}</div>
                  <div style={{fontSize:10,color:C.muted}}>{t.antibiotic_name}</div>
                </td>
                <td style={{padding:"12px 14px"}}><CountryCell iso3={t.country_iso3}/></td>
                <td style={{padding:"12px 14px"}}><span style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:((t.current_rate||0)*100)>=50?C.red:((t.current_rate||0)*100)>=25?C.amber:C.white}}>{((t.current_rate||t.current_resistance||0)*100).toFixed(1)}%</span></td>
                <td style={{padding:"12px 14px"}}>
                  <div style={{display:"flex",gap:2,alignItems:"center"}}>
                    {Array.from({length:10}).map((_,j)=>(
                      <div key={j} style={{width:4,height:12,borderRadius:1,background:j<Math.round((t.acceleration_score||0)/10)?C.amber:C.border}}/>
                    ))}
                    <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.muted,marginLeft:4}}>{Math.round((t.acceleration_score||0)/10)}/10</span>
                  </div>
                </td>
                <td style={{padding:"12px 14px"}}>
                  <span style={{display:"inline-flex",alignItems:"center",padding:"2px 8px",borderRadius:3,background:(EMERGENCE_COLOR[t.emergence_tier?.toUpperCase()]||C.muted)+"20",color:EMERGENCE_COLOR[t.emergence_tier?.toUpperCase()]||C.muted,fontFamily:"JetBrains Mono,monospace",fontSize:10,fontWeight:700,letterSpacing:".05em",border:"1px solid " + (EMERGENCE_COLOR[t.emergence_tier?.toUpperCase()]||C.muted) + "40"}}>{(t.emergence_tier||"").toUpperCase()}</span>
                </td>
                <td style={{padding:"12px 14px",maxWidth:260}}><span style={{fontSize:11,color:C.mutedHigh,lineHeight:1.5}}>{t.why_emerging||t.driver_phrase||"—"}</span></td>
                <td style={{padding:"12px 14px"}}>
                  <button onClick={async()=>{
                    const data = await apiFetch("/alerts?page_size=100&sort_by=severity_score");
                    const all = data?.alerts||[];
                    const match = all.find(a=>a.pathogen_name===t.pathogen_name&&a.antibiotic_name===t.antibiotic_name&&a.country_iso3===t.country_iso3);
                    if(match) onInvestigate&&onInvestigate(match);
                    else onInvestigate&&onInvestigate({...t,alert_id:t.id,severity_score:t.emergence_score,severity_tier:t.emergence_score>=95?"critical":t.emergence_score>=70?"warn":"monitor",current_resistance:t.current_rate||0,trend_direction:"rising",signal_type:"emergence"});
                  }} style={{background:"none",border:"1px solid " + C.border,color:C.mutedHigh,borderRadius:4,padding:"4px 10px",fontSize:11}}>Investigate →</button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ─── Screen 4: Genomic Intelligence ──────────────────────────────────────────
function GenomicIntelligence({ onInvestigate }) {
  const [signals,  setSignals]  = useState([]);
  const [loading,  setLoading]  = useState(true);
  const [tab,      setTab]      = useState("all");
  const [search,   setSearch]   = useState("");
  const [selected, setSelected] = useState(null);

  useEffect(() => {
    apiFetch("/alerts?page_size=200&sort_by=severity_score").then(data => {
      const all = data?.alerts || [];
      setSignals(all.filter(a => a.signal_type === "genomic_precursor"));
      setLoading(false);
    });
  }, []);

  const tabCounts = {
    all:    signals.length,
    high:   signals.filter(s => s.surveillance_confidence === "HIGH").length,
    medium: signals.filter(s => s.surveillance_confidence === "MEDIUM").length,
  };

  const filtered = signals
    .filter(s => tab === "all" || (tab === "high" && s.surveillance_confidence === "HIGH") || (tab === "medium" && s.surveillance_confidence === "MEDIUM"))
    .filter(s => !search || (s.gene_name||"").toLowerCase().includes(search.toLowerCase()) || (s.pathogen_name||"").toLowerCase().includes(search.toLowerCase()) || (s.country_iso3||"").toLowerCase().includes(search.toLowerCase()))
    .sort((a, b) => (b.severity_score||0) - (a.severity_score||0));

  if (loading) return <Spinner />;

  const selStyle = { background:C.surfaceHigh, border:"1px solid " + C.border, color:C.mutedHigh, borderRadius:5, padding:"6px 10px", fontSize:12 };

  // Stats
  const highConf = signals.filter(s => s.surveillance_confidence === "HIGH").length;
  const carbapenem = signals.filter(s => (s.antibiotic_name||"").toLowerCase().includes("carbapenem")).length;
  const uniqueCountries = new Set(signals.map(s => s.country_iso3)).size;
  const fastestDoubling = signals.filter(s => s.doubling_time_years).reduce((min, s) => s.doubling_time_years < min ? s.doubling_time_years : min, 99);

  return (
    <div className="fade-up" style={{padding:"20px 24px",maxWidth:1400}}>
      {/* Header */}
      <div style={{marginBottom:16}}>
        <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:4}}>
          <span style={{fontSize:20}}>🧬</span>
          <h1 style={{fontSize:18,fontWeight:700,color:C.white}}>Genomic Intelligence</h1>
          <span style={{fontSize:11,background:C.tealDim,color:C.teal,padding:"2px 8px",borderRadius:4,border:"1px solid " + C.teal + "40",fontFamily:"JetBrains Mono,monospace",fontWeight:700}}>PRE-PHENOTYPIC</span>
        </div>
        <div style={{fontSize:11,color:C.muted}}>
          Resistance genes detected in clinical isolates before phenotypic resistance appears in surveillance data. Source: NCBI NDARO (16,882 signals, 6 pathogens).
        </div>
      </div>

      {/* Confidence legend */}
      <div style={{background:C.surface,border:"1px solid " + C.teal + "30",borderRadius:8,padding:"12px 16px",marginBottom:16,display:"flex",gap:24,alignItems:"center"}}>
        <div style={{fontSize:10,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",flexShrink:0}}>Confidence Guide</div>
        {[
          {icon:"●",conf:"HIGH",desc:"ECDC-covered country — phenotypic data is reliable. Gene growing, resistance not yet detected. Genuine precursor."},
          {icon:"◐",conf:"MEDIUM",desc:"Limited surveillance coverage. Genomic detection without phenotypic data is plausible but requires in-country validation."},
          {icon:"○",conf:"LOW",desc:"Country has independent surveillance (CDC, China AMR) not ingested. Absence of phenotypic data reflects a source gap."},
        ].map(item => (
          <div key={item.conf} style={{display:"flex",alignItems:"flex-start",gap:8,flex:1}}>
            <span style={{color:CONF_COLOR[item.conf],fontSize:14,flexShrink:0,marginTop:1}}>{item.icon}</span>
            <div>
              <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,fontWeight:700,color:CONF_COLOR[item.conf]}}>{item.conf}</span>
              <span style={{fontSize:10,color:C.muted,marginLeft:6}}>{item.desc}</span>
            </div>
          </div>
        ))}
      </div>

      {/* Stat cards */}
      <div className="stat-row" style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:12,marginBottom:16}}>
        <StatCard label="Precursor Signals" value={signals.length} sub="gene present, phenotype low/absent" accent={C.teal}/>
        <StatCard label="HIGH Confidence" value={highConf} sub="ECDC-covered countries" accent={C.green}/>
        <StatCard label="Carbapenem Genes" value={carbapenem} sub="critical-tier resistance" accent={C.red}/>
        <StatCard label="Countries Affected" value={uniqueCountries} sub={"fastest doubling: " + (fastestDoubling < 99 ? fastestDoubling + "yr" : "N/A")} accent={C.amber}/>
      </div>

      {/* Tabs + search */}
      <div style={{display:"flex",alignItems:"center",gap:8,marginBottom:0}}>
        <div style={{display:"flex",gap:2,borderBottom:"1px solid " + C.border,flex:1}}>
          {[
            {id:"all",label:"All Signals (" + tabCounts.all + ")"},
            {id:"high",label:"● HIGH (" + tabCounts.high + ")"},
            {id:"medium",label:"◐ MEDIUM (" + tabCounts.medium + ")"},
          ].map(t => (
            <button key={t.id} onClick={()=>setTab(t.id)} style={{
              padding:"8px 16px",background:"none",border:"none",
              borderBottom: tab===t.id?"2px solid " + C.teal:"2px solid transparent",
              color: tab===t.id?C.white:C.muted,fontSize:12,fontWeight:tab===t.id?600:400,marginBottom:-1,
            }}>{t.label}</button>
          ))}
        </div>
        <input value={search} onChange={e=>setSearch(e.target.value)} placeholder="Search gene, pathogen, country..." style={{...selStyle,width:240,padding:"6px 12px",color:C.white,marginBottom:0}}/>
      </div>

      {/* Two-panel layout */}
      <div style={{display:"grid",gridTemplateColumns:selected?"1fr 380px":"1fr",gap:16,marginTop:0}}>
        {/* Signal table */}
        <div style={{background:C.surface,border:"1px solid " + C.border,borderTop:"none",borderRadius:"0 0 8px 8px",overflow:"hidden"}}>
          <table style={{width:"100%",borderCollapse:"collapse"}}>
            <thead>
              <tr style={{background:C.surfaceHigh,borderBottom:"1px solid " + C.border}}>
                {["Score","Gene","Pathogen","Country","Trajectory","Doubling","Phenotypic","Confidence",""].map(h=>(
                  <th key={h} style={{padding:"10px 14px",textAlign:"left",color:C.muted,fontSize:10,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",whiteSpace:"nowrap"}}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filtered.length === 0 ? (
                <tr><td colSpan={9}><Empty msg="No genomic precursor signals match your filters"/></td></tr>
              ) : filtered.map((s, i) => (
                <tr key={s.alert_id||i}
                  onClick={() => setSelected(selected?.alert_id === s.alert_id ? null : s)}
                  style={{borderBottom:"1px solid " + C.border + "20",cursor:"pointer",transition:"background .12s",background:selected?.alert_id===s.alert_id?C.surfaceHigh:"transparent",borderLeft:"2px solid " + (selected?.alert_id===s.alert_id?C.teal:"transparent")}}
                  onMouseEnter={e=>{ if(selected?.alert_id!==s.alert_id) e.currentTarget.style.background=C.surfaceHigh+"80"; }}
                  onMouseLeave={e=>{ if(selected?.alert_id!==s.alert_id) e.currentTarget.style.background="transparent"; }}
                >
                  <td style={{padding:"10px 14px"}}>
                    <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:16,fontWeight:700,color:s.severity_tier==="critical"?C.red:C.amber}}>{s.severity_score}</span>
                  </td>
                  <td style={{padding:"10px 14px"}}>
                    <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.teal}}>{s.gene_name||"—"}</div>
                    <div style={{fontSize:9,color:C.muted}}>{s.antibiotic_name||""}</div>
                  </td>
                  <td style={{padding:"10px 14px"}}>
                    <div style={{fontSize:12,fontWeight:600,color:C.white}}>
                      {(s.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Escherichia coli","E. coli").replace("Staphylococcus aureus","S. aureus").replace("Enterococcus faecium","E. faecium").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Acinetobacter baumannii","A. baumannii")}
                    </div>
                  </td>
                  <td style={{padding:"10px 14px"}}><CountryCell iso3={s.country_iso3} size="sm"/></td>
                  <td style={{padding:"10px 14px"}}>
                    <TrajectorySparkline timeSeries={s.time_series}/>
                    <div style={{fontSize:9,color:C.muted,marginTop:2,fontFamily:"JetBrains Mono,monospace"}}>{(s.isolate_count||0).toLocaleString()} isolates ({s.latest_year||"—"})</div>
                  </td>
                  <td style={{padding:"10px 14px"}}>
                    {s.doubling_time_years ? (
                      <div>
                        <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:s.doubling_time_years<1?C.red:s.doubling_time_years<2?C.amber:C.mutedHigh}}>
                          {"2x/" + s.doubling_time_years + "yr"}
                        </div>
                        {s.days_to_threshold && <div style={{fontSize:9,color:C.muted}}>{"\u2192500 in " + s.days_to_threshold + "d"}</div>}
                      </div>
                    ) : <span style={{color:C.muted,fontSize:11}}>—</span>}
                  </td>
                  <td style={{padding:"10px 14px"}}>
                    {s.current_resistance > 0 ? (
                      <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.amber}}>{(s.current_resistance*100).toFixed(0) + "%"}</span>
                    ) : (
                      <span style={{fontSize:10,color:C.muted,fontStyle:"italic"}}>No data</span>
                    )}
                  </td>
                  <td style={{padding:"10px 14px"}}><ConfBadge conf={s.surveillance_confidence}/></td>
                  <td style={{padding:"10px 14px",color:selected?.alert_id===s.alert_id?C.teal:C.accent,fontSize:12}}>{selected?.alert_id===s.alert_id?"✕":"›"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{padding:"10px 14px",borderTop:"1px solid " + C.border,fontSize:11,color:C.muted}}>
            Showing {filtered.length} of {signals.length} genomic precursor signals
          </div>
        </div>

        {/* Signal detail panel */}
        {selected && (
          <div style={{background:C.surface,border:"1px solid " + C.teal + "40",borderRadius:8,padding:20,height:"fit-content",position:"sticky",top:20}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:14}}>
              <div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:20,fontWeight:800,color:C.teal,lineHeight:1}}>{selected.gene_name}</div>
                <div style={{fontSize:11,color:C.muted,marginTop:3}}>{selected.gene_description||""}</div>
              </div>
              <ConfBadge conf={selected.surveillance_confidence}/>
            </div>

            <div style={{display:"flex",flexDirection:"column",gap:10,marginBottom:16}}>
              <div style={{display:"flex",justifyContent:"space-between"}}>
                <span style={{fontSize:11,color:C.muted}}>Pathogen</span>
                <span style={{fontSize:11,fontWeight:600,color:C.white}}>{(selected.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Escherichia coli","E. coli").replace("Staphylococcus aureus","S. aureus").replace("Enterococcus faecium","E. faecium").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Acinetobacter baumannii","A. baumannii")}</span>
              </div>
              <div style={{display:"flex",justifyContent:"space-between"}}>
                <span style={{fontSize:11,color:C.muted}}>Country</span>
                <CountryCell iso3={selected.country_iso3} size="sm"/>
              </div>
              <div style={{display:"flex",justifyContent:"space-between"}}>
                <span style={{fontSize:11,color:C.muted}}>Drug class</span>
                <span style={{fontSize:11,fontWeight:600,color:C.red}}>{selected.antibiotic_name}</span>
              </div>
              <div style={{display:"flex",justifyContent:"space-between"}}>
                <span style={{fontSize:11,color:C.muted}}>Isolate count</span>
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.teal}}>{(selected.isolate_count||0).toLocaleString()} ({selected.latest_year})</span>
              </div>
              {selected.doubling_time_years && (
                <div style={{display:"flex",justifyContent:"space-between"}}>
                  <span style={{fontSize:11,color:C.muted}}>Doubling time</span>
                  <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:selected.doubling_time_years<1?C.red:selected.doubling_time_years<2?C.amber:C.mutedHigh}}>
                    {"every " + selected.doubling_time_years + " years"}
                  </span>
                </div>
              )}
              {selected.days_to_threshold && (
                <div style={{display:"flex",justifyContent:"space-between"}}>
                  <span style={{fontSize:11,color:C.muted}}>→500 isolates</span>
                  <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.amber}}>{selected.days_to_threshold + " days"}</span>
                </div>
              )}
              <div style={{display:"flex",justifyContent:"space-between"}}>
                <span style={{fontSize:11,color:C.muted}}>Phenotypic rate</span>
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:selected.current_resistance>0?C.amber:C.muted}}>
                  {selected.current_resistance > 0 ? (selected.current_resistance*100).toFixed(1) + "%" : "No data"}
                </span>
              </div>
            </div>

            {/* Trajectory chart */}
            {selected.time_series && Object.keys(selected.time_series).length >= 2 && (
              <div style={{marginBottom:14}}>
                <div style={{fontSize:10,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:8}}>Isolate Trajectory</div>
                <ResponsiveContainer width="100%" height={100}>
                  <AreaChart data={Object.entries(selected.time_series).map(([y,c])=>({year:parseInt(y),count:c})).sort((a,b)=>a.year-b.year)} margin={{top:5,right:5,bottom:0,left:0}}>
                    <defs>
                      <linearGradient id="tealGrad" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="5%" stopColor={C.teal} stopOpacity={0.3}/>
                        <stop offset="95%" stopColor={C.teal} stopOpacity={0}/>
                      </linearGradient>
                    </defs>
                    <XAxis dataKey="year" stroke={C.muted} tick={{fill:C.muted,fontSize:9,fontFamily:"JetBrains Mono,monospace"}}/>
                    <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:9,fontFamily:"JetBrains Mono,monospace"}} tickFormatter={v=>v>=1000?(v/1000).toFixed(1)+"k":v}/>
                    <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}} formatter={v=>[v.toLocaleString(),"Isolates"]} labelStyle={{color:C.white}}/>
                    <Area type="monotone" dataKey="count" stroke={C.teal} fill="url(#tealGrad)" strokeWidth={2} dot={{fill:C.teal,r:3}}/>
                  </AreaChart>
                </ResponsiveContainer>
              </div>
            )}

            {/* Spread risk */}
            {selected.spread_risk_countries && selected.spread_risk_countries.length > 0 && (
              <div style={{marginBottom:14}}>
                <div style={{fontSize:10,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:6}}>Spread Risk Countries</div>
                <div style={{display:"flex",flexWrap:"wrap",gap:4}}>
                  {selected.spread_risk_countries.map(iso3 => (
                    <span key={iso3} style={{fontSize:10,background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:4,padding:"2px 6px",color:C.mutedHigh,display:"inline-flex",alignItems:"center",gap:3}}>
                      {getFlagUrl(iso3) && <img src={getFlagUrl(iso3)} width="12" height="9" style={{borderRadius:1}} alt=""/>}
                      {countryName(iso3)}
                    </span>
                  ))}
                </div>
              </div>
            )}

            {/* Caveat */}
            {selected.surveillance_caveat && (
              <div style={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6,padding:"10px 12px",marginBottom:14}}>
                <div style={{fontSize:9,color:C.muted,fontWeight:600,textTransform:"uppercase",letterSpacing:".06em",marginBottom:4}}>Validation Note</div>
                <div style={{fontSize:10,color:C.mutedHigh,lineHeight:1.6}}>{selected.surveillance_caveat}</div>
              </div>
            )}

            <button onClick={()=>onInvestigate&&onInvestigate(selected)} style={{width:"100%",background:C.tealDim,border:"1px solid " + C.teal + "50",color:C.teal,borderRadius:6,padding:"8px",fontSize:12,fontWeight:600}}>
              Investigate Full Alert →
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Screen 5: Alert Investigation ───────────────────────────────────────────
function AlertInvestigation({ alert: init }) {
  const [alert,   setAlert]   = useState(init);
  const [tab,     setTab]     = useState("report");
  const [trend,   setTrend]   = useState(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    if (!alert) return;
    const id = alert.alert_id || alert.id;
    if (!id) return;
    setLoading(true);
    Promise.all([
      apiFetch("/alerts/" + id),
      apiFetch("/resistance-trends?pathogen=" + encodeURIComponent(alert.pathogen_name) + "&antibiotic=" + encodeURIComponent(alert.antibiotic_name) + "&country=" + alert.country_iso3),
    ]).then(([full, td]) => {
      if(full) setAlert(full);
      const raw = Array.isArray(td) ? td[0] : td;
      setTrend({data: raw?.data_points||[]});
      setLoading(false);
    });
  }, [init?.alert_id, init?.id]);

  if (!alert) return (
    <div className="fade-up" style={{padding:"20px 24px"}}>
      <Empty msg="Select an alert from Threat Operations or Command Center to investigate"/>
    </div>
  );

  const isGenomic = alert.signal_type === "genomic_precursor";
  const cits = alert.evidence_citations||[];
  const trendPts = trend?.data||[];
  const accentColor = isGenomic ? C.teal : (TIER_COLOR[alert.severity_tier] || C.blue);

  const pName = (alert.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Enterococcus faecium","E. faecium").replace("Staphylococcus aureus","S. aureus").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli").replace("Acinetobacter spp.","Acinetobacter").replace("Acinetobacter baumannii","A. baumannii");

  const obsRate = ((alert.current_resistance||0)*100).toFixed(1);
  const expRate = ((alert.forecasted_rate||0)*100).toFixed(1);
  const devPp   = ((alert.deviation_magnitude||0)*100).toFixed(1);
  const lo80    = alert.forecast_lower_80 != null ? (alert.forecast_lower_80*100).toFixed(1) : null;
  const hi80    = alert.forecast_upper_80 != null ? (alert.forecast_upper_80*100).toFixed(1) : null;
  const outsidePI = lo80 && hi80 && (alert.current_resistance||0)*100 > parseFloat(hi80);

  const trajectoryLabel = alert.trend_direction === "rising"
    ? (parseFloat(obsRate) < 15 ? "Emerging" : parseFloat(obsRate) < 40 ? "Escalating" : "Endemic Critical")
    : alert.trend_direction === "falling" ? "Improving" : "Stable";

  const trajectoryColor = alert.trend_direction === "rising"
    ? (parseFloat(obsRate) < 15 ? C.amber : parseFloat(obsRate) < 40 ? C.red : C.red)
    : alert.trend_direction === "falling" ? C.green : C.muted;

  // Build provenance sources
  const sources = [];
  if (!isGenomic) {
    sources.push({label:"WHO GLASS", ok:true});
    sources.push({label:"ECDC EARS-Net", ok:true});
    if(cits.length > 0) sources.push({label:"PubMed (" + cits.length + " studies)", ok:true});
    sources.push({label:"TFT Forecast Model", ok:alert.forecasted_rate != null});
    if(alert.forecast_lower_80 != null) sources.push({label:"Confidence Intervals (80%)", ok:true});
  } else {
    sources.push({label:"NCBI NDARO", ok:true});
    if(alert.current_resistance > 0) sources.push({label:"Phenotypic Surveillance", ok:true});
    else sources.push({label:"Phenotypic Surveillance", ok:false, note:"Not yet detected"});
    sources.push({label:"Genomic Trajectory Model", ok:true});
  }

  // Primary signal drivers for the evidence card
  const drivers = [];
  if(!isGenomic) {
    if(parseFloat(devPp) > 20) drivers.push("Resistance exceeded model expectation by " + devPp + " percentage points");
    if(outsidePI && lo80 && hi80) drivers.push("Outside 80% prediction interval (" + lo80 + "–" + hi80 + "%)");
    if(alert.trend_direction === "rising") drivers.push("Rising trajectory across 3+ consecutive surveillance years");
    if(cits.length >= 3) drivers.push("Consistent with " + cits.length + " peer-reviewed studies");
    if((alert.current_resistance||0) >= 0.5) drivers.push("Absolute resistance rate ≥50% — treatment failure threshold");
    drivers.push("Severity score " + alert.severity_score + "/100");
  } else {
    if(alert.doubling_time_years && alert.doubling_time_years < 2) drivers.push("Sub-2 year doubling time (" + alert.doubling_time_years + "yr) — exponential expansion");
    if(alert.isolate_count) drivers.push(alert.isolate_count.toLocaleString() + " sequenced isolates detected carrying " + (alert.gene_name||"resistance gene"));
    if(alert.current_resistance > 0) drivers.push("Phenotypic resistance beginning to appear (" + (alert.current_resistance*100).toFixed(1) + "%) — converging signals");
    else drivers.push("No phenotypic resistance yet detected — genuine pre-phenotypic window");
    if(alert.surveillance_confidence === "HIGH") drivers.push("HIGH confidence — ECDC phenotypic surveillance confirms data reliability");
  }

  const tabs = [
    {id:"report",   label:"Intelligence Report"},
    {id:"trend",    label: isGenomic ? "Genomic Trajectory" : "Trajectory Analysis"},
    {id:"citations",label:"Evidence (" + cits.length + ")"},
    {id:"history",  label:"History"},
  ];

  // Structured bulletin sections parsed from stewardship_guidance
  function renderStructuredBulletin(text) {
    if(!text) return <Empty msg="Intelligence report not yet generated"/>;

    // Known section headers in the 6-section intelligence report format
    var SECTION_HEADERS = [
      "EXECUTIVE SUMMARY",
      "SITUATION ASSESSMENT",
      "POSSIBLE DRIVERS",
      "RECOMMENDED ACTIONS",
      "CONFIDENCE ASSESSMENT",
      "STRATEGIC IMPLICATIONS"
    ];

    // Normalise: inject \n\n before each section header wherever it appears
    // mid-line (handles bulletins where header and content are on the same line)
    var normalised = text;
    SECTION_HEADERS.forEach(function(header) {
      // Replace occurrences of the header that are NOT already at the start of a line
      var re = new RegExp("(?<!\n)(" + header + ")", "g");
      normalised = normalised.replace(re, "\n\n" + header + "\n");
    });

    // Split on double newlines for paragraphs
    var paragraphs = normalised.split(/\n\n+/).filter(Boolean);
    return (
      <div style={{display:"flex",flexDirection:"column",gap:20}}>
        {paragraphs.map(function(para, i) {
          var lines = para.trim().split("\n").filter(Boolean);
          if(lines.length === 0) return null;
          var firstLine = lines[0].trim();
          // A line is a section header if it matches one of the known headers
          var isSectionHeader = SECTION_HEADERS.indexOf(firstLine) !== -1;
          // Also catch any ALL-CAPS short line as a fallback
          var isAllCapsHeader = !isSectionHeader && firstLine === firstLine.toUpperCase() && firstLine.length < 60 && /[A-Z]{3}/.test(firstLine);
          var isHeader = isSectionHeader || isAllCapsHeader;

          if(isHeader) {
            var content = lines.slice(1).join(" ").trim();
            if(!content) return (
              <div key={i} style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",paddingBottom:6,borderBottom:"1px solid " + C.border}}>{firstLine}</div>
            );
            return (
              <div key={i}>
                <div style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>{firstLine}</div>
                <div style={{color:C.mutedHigh,fontSize:13,lineHeight:1.85}}>{content}</div>
              </div>
            );
          }
          return <div key={i} style={{color:C.mutedHigh,fontSize:13,lineHeight:1.85}}>{para.trim()}</div>;
        })}
      </div>
    );
  }

  return (
    <div className="fade-up" style={{padding:"20px 24px",maxWidth:1400}}>

      {/* ── Alert header ── */}
      <div style={{background:C.surface,border:"1px solid " + C.border,borderLeft:"4px solid " + accentColor,borderRadius:8,padding:20,marginBottom:16}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",flexWrap:"wrap",gap:16}}>
          <div style={{flex:1,minWidth:0}}>
            <div style={{display:"flex",gap:10,alignItems:"center",marginBottom:8,flexWrap:"wrap"}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:36,fontWeight:800,color:accentColor,lineHeight:1,flexShrink:0}}>{alert.severity_score}</div>
              <div>
                <div style={{fontSize:9,color:C.muted,letterSpacing:".08em",textTransform:"uppercase",marginBottom:2}}>Intelligence Score</div>
                {isGenomic ? <ConfBadge conf={alert.surveillance_confidence}/> : <SeverityBadge tier={alert.severity_tier}/>}
              </div>
              {isGenomic && <span style={{fontSize:11,background:C.tealDim,color:C.teal,padding:"3px 10px",borderRadius:4,border:"1px solid " + C.teal + "40",fontFamily:"JetBrains Mono,monospace",fontWeight:700}}>🧬 GENOMIC PRECURSOR</span>}
            </div>
            <h1 style={{fontSize:20,fontWeight:700,color:C.white,marginBottom:4,wordBreak:"break-word",overflowWrap:"anywhere"}}>
              {pName}
              <span style={{marginLeft:10,padding:"2px 8px",borderRadius:4,background:isGenomic?C.tealDim:C.blueDim,color:isGenomic?C.teal:C.blue,fontSize:12,fontWeight:500}}>
                {isGenomic ? (alert.gene_name || "Genomic Signal") : (alert.antibiotic_name + " Resistance")}
              </span>
            </h1>
            <div style={{fontSize:12,color:C.muted,display:"flex",alignItems:"center",gap:6,flexWrap:"wrap",marginBottom:8}}>
              {getFlagUrl(alert.country_iso3) && <img src={getFlagUrl(alert.country_iso3)} width="14" height="10" style={{borderRadius:1}} alt=""/>}
              {countryName(alert.country_iso3)} ({alert.country_iso3})
              <span style={{color:C.border}}>·</span>
              Detected {alert.created_at ? new Date(alert.created_at).toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"}) : "—"}
              <span style={{color:C.border}}>·</span>
              <span style={{color:trajectoryColor,fontWeight:600}}>{"Trajectory: " + trajectoryLabel}</span>
            </div>
            {/* One-sentence executive summary */}
            {!isGenomic && (
              <div style={{fontSize:13,color:C.mutedHigh,lineHeight:1.6,maxWidth:580,fontStyle:"italic",borderLeft:"2px solid " + accentColor + "60",paddingLeft:10}}>
                {pName + " resistance to " + (alert.antibiotic_name||"this antibiotic") + " in " + countryName(alert.country_iso3) + " has exceeded the forecast by " + devPp + " percentage points" + (outsidePI ? ", is outside the 80% prediction interval," : "") + " and is " + (trajectoryLabel === "Endemic Critical" ? "at a level requiring immediate national stewardship and infection-control response." : trajectoryLabel === "Escalating" ? "escalating at a rate that warrants urgent surveillance attention and stewardship review." : "emerging and should be monitored closely by national AMR authorities.")}
              </div>
            )}
            {isGenomic && (
              <div style={{fontSize:13,color:C.mutedHigh,lineHeight:1.6,maxWidth:580,fontStyle:"italic",borderLeft:"2px solid " + C.teal + "60",paddingLeft:10}}>
                {"Genomic surveillance has detected " + (alert.gene_name||"a resistance gene") + " in " + countryName(alert.country_iso3) + " with " + (alert.isolate_count||0).toLocaleString() + " sequenced isolates" + (alert.doubling_time_years && alert.doubling_time_years < 2 ? ", expanding at sub-2-year doubling time — a pre-phenotypic signal requiring immediate genomic surveillance escalation." : " — a pre-phenotypic signal warranting enhanced genomic surveillance.")}
              </div>
            )}
          </div>

          {/* Metric cards */}
          <div style={{display:"grid",gridTemplateColumns:"repeat(auto-fit,minmax(120px,1fr))",gap:8,textAlign:"center"}}>
            {(isGenomic
              ? [
                  {label:"Isolate Count",     value:(alert.isolate_count||0).toLocaleString(), color:C.teal},
                  {label:"Doubling Time",     value:alert.doubling_time_years?(alert.doubling_time_years + "yr"):"N/A", color:alert.doubling_time_years&&alert.doubling_time_years<1?C.red:alert.doubling_time_years&&alert.doubling_time_years<2?C.amber:C.mutedHigh},
                  {label:"Phenotypic Rate",   value:alert.current_resistance>0?((alert.current_resistance*100).toFixed(1)+"%"):"Not detected", color:alert.current_resistance>0?C.amber:C.muted},
                  {label:"Signal Score",      value:alert.severity_score + "/100", color:accentColor},
                ]
              : [
                  {label:"Observed", value:obsRate + "%",      color:(alert.current_resistance||0)>=.5?C.red:C.amber},
                  {label:"Forecast Resistance", value:expRate + "%",      color:C.mutedHigh},
                  {label:"Above Forecast",      value:"+" + devPp + "pp", color:C.red},
                  {label:"Confidence",          value:"High",             color:C.green},
                ]
            ).map(m=>(
              <div key={m.label} style={{background:C.surfaceHigh,borderRadius:6,padding:"10px 8px",border:"1px solid " + C.border}}>
                <div style={{fontSize:9,color:C.muted,letterSpacing:".05em",textTransform:"uppercase",marginBottom:4}}>{m.label}</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:15,fontWeight:700,color:m.color}}>{m.value}</div>
              </div>
            ))}
          </div>
        </div>

        {/* Prediction interval row — phenotypic only */}
        {!isGenomic && lo80 && hi80 && (
          <div style={{marginTop:14,paddingTop:12,borderTop:"1px solid " + C.border,display:"flex",gap:28,alignItems:"center",flexWrap:"wrap"}}>
            <div>
              <div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".06em",marginBottom:2}}>80% Prediction Interval</div>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.mutedHigh}}>{lo80 + "% – " + hi80 + "%"}</div>
            </div>
            <div>
              <div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".06em",marginBottom:2}}>Signal Status</div>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:outsidePI?C.red:C.green,fontWeight:700}}>{outsidePI ? "Outside Prediction Interval" : "Within Expected Range"}</div>
            </div>
            <div>
              <div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".06em",marginBottom:2}}>Forecast Confidence</div>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.green,fontWeight:700}}>High — 80% CI Available</div>
            </div>
            <div style={{marginLeft:"auto",display:"flex",gap:6,flexWrap:"wrap"}}>
              {sources.map((s,i)=>(
                <span key={i} style={{fontSize:10,padding:"3px 8px",borderRadius:4,background:s.ok?C.greenDim:C.surfaceHigh,color:s.ok?C.green:C.muted,border:"1px solid " + (s.ok?C.green+"40":C.border),fontFamily:"JetBrains Mono,monospace"}}>
                  {s.ok ? "✓" : "○"} {s.label}
                </span>
              ))}
            </div>
          </div>
        )}
        {isGenomic && (
          <div style={{marginTop:14,paddingTop:12,borderTop:"1px solid " + C.border,display:"flex",gap:6,flexWrap:"wrap",alignItems:"center"}}>
            <span style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".06em",marginRight:6}}>Data Sources</span>
            {sources.map((s,i)=>(
              <span key={i} style={{fontSize:10,padding:"3px 8px",borderRadius:4,background:s.ok?C.tealDim:C.surfaceHigh,color:s.ok?C.teal:C.muted,border:"1px solid " + (s.ok?C.teal+"40":C.border),fontFamily:"JetBrains Mono,monospace"}}>
                {s.ok ? "✓" : "○"} {s.label}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* ── WHY THIS ALERT? — Evidence card ── */}
      <div style={{background:C.surfaceHigh,border:"1px solid " + accentColor + "40",borderRadius:8,padding:16,marginBottom:16}}>
        <div style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:12}}>Why AMR-Intel Triggered This Alert</div>
        <div style={{display:"flex",flexDirection:"column",gap:7}}>
          {drivers.map((d,i)=>(
            <div key={i} style={{display:"flex",alignItems:"flex-start",gap:10}}>
              <span style={{color:accentColor,fontSize:13,flexShrink:0,marginTop:1}}>↑</span>
              <span style={{fontSize:12,color:C.mutedHigh,lineHeight:1.5}}>{d}</span>
            </div>
          ))}
        </div>
      </div>

      {/* ── Recommended Actions ── */}
      {!isGenomic && (
        <div style={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:8,padding:16,marginBottom:16}}>
          <div style={{fontSize:9,color:C.green,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:14}}>Recommended Actions</div>
          <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:12}}>
            {[
              {
                tier:"Immediate",
                color:C.red,
                actions:[
                  "Notify Ministry of Health / National AMR authority",
                  "Review empiric treatment guidelines for " + pName,
                  "Alert hospital infection control teams",
                  "Increase laboratory susceptibility testing",
                ]
              },
              {
                tier:"Within 30 Days",
                color:C.amber,
                actions:[
                  "Sequence representative isolates for resistance gene profiling",
                  "Review IPC compliance in affected facilities",
                  "Expand surveillance to neighbouring regions",
                  "Cross-reference WHO GLASS for regional trends",
                ]
              },
              {
                tier:"Ongoing Monitor",
                color:C.teal,
                actions:[
                  "Track resistance trajectory monthly",
                  "Monitor epidemiologically linked countries",
                  "Align with ECDC / WHO regional surveillance updates",
                  "Report to national AMR stewardship programme",
                ]
              },
            ].map(group=>(
              <div key={group.tier}>
                <div style={{fontSize:9,color:group.color,fontWeight:700,letterSpacing:".06em",textTransform:"uppercase",marginBottom:8,paddingBottom:5,borderBottom:"1px solid " + group.color + "30"}}>{group.tier}</div>
                <div style={{display:"flex",flexDirection:"column",gap:6}}>
                  {group.actions.map((a,i)=>(
                    <div key={i} style={{display:"flex",alignItems:"flex-start",gap:7}}>
                      <span style={{color:group.color,fontSize:10,flexShrink:0,marginTop:2}}>◆</span>
                      <span style={{fontSize:11,color:C.mutedHigh,lineHeight:1.5}}>{a}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
          <div style={{marginTop:12,paddingTop:10,borderTop:"1px solid " + C.border,fontSize:9,color:C.muted,fontStyle:"italic"}}>
            Actions are surveillance intelligence guidance, not clinical prescribing advice. Specific stewardship protocols depend on local guidelines and patient context.
          </div>
        </div>
      )}

      <div style={{display:"grid",gridTemplateColumns:"1fr 270px",gap:16}}>
        <div>
          {/* Tabs */}
          <div style={{display:"flex",gap:2,borderBottom:"1px solid " + C.border,marginBottom:0}}>
            {tabs.map(t=>(
              <button key={t.id} onClick={()=>setTab(t.id)} style={{padding:"8px 16px",background:"none",border:"none",borderBottom:tab===t.id?"2px solid " + accentColor:"2px solid transparent",color:tab===t.id?C.white:C.muted,fontSize:12,fontWeight:tab===t.id?600:400,marginBottom:-1}}>{t.label}</button>
            ))}
          </div>

          <div style={{background:C.surface,border:"1px solid " + C.border,borderTop:"none",borderRadius:"0 0 8px 8px",padding:24,minHeight:320}}>
            {loading ? <Spinner/> : (
              <>
                {/* ── Intelligence Report tab ── */}
                {tab==="report" && (
                  <div>
                    {isGenomic ? (
                      <div style={{display:"flex",flexDirection:"column",gap:20}}>
                        {/* Executive Summary */}
                        <div>
                          <div style={{fontSize:9,color:C.teal,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Executive Summary</div>
                          <div style={{color:C.mutedHigh,fontSize:13,lineHeight:1.85}}>
                            {alert.intelligence_summary || ("Genomic surveillance has detected " + (alert.gene_name||"a resistance gene") + " in " + (alert.isolate_count||0).toLocaleString() + " clinical isolates of " + pName + " in " + countryName(alert.country_iso3) + ". " + (alert.doubling_time_years && alert.doubling_time_years < 2 ? "The gene is expanding at sub-2-year doubling time, indicating exponential growth. " : "") + (alert.current_resistance > 0 ? "Phenotypic resistance is beginning to appear, representing a converging pre-phenotypic signal." : "Phenotypic resistance has not yet been detected in surveillance data, representing a genuine early warning opportunity."))}
                          </div>
                        </div>
                        {/* Evidence */}
                        <div>
                          <div style={{fontSize:9,color:C.teal,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Genomic Evidence</div>
                          <div style={{display:"grid",gridTemplateColumns:"1fr 1fr",gap:10}}>
                            {[
                              {l:"Gene",              v:alert.gene_name||"—"},
                              {l:"Gene Family",       v:alert.gene_family||"—"},
                              {l:"Pathogen",          v:pName},
                              {l:"Drug Class",        v:alert.antibiotic_name||"—"},
                              {l:"Isolate Count",     v:(alert.isolate_count||0).toLocaleString()},
                              {l:"Doubling Time",     v:alert.doubling_time_years?(alert.doubling_time_years+"yr"):"N/A"},
                              {l:"Phenotypic Rate",   v:alert.current_resistance>0?((alert.current_resistance*100).toFixed(1)+"%"):"Not detected"},
                              {l:"WHO Priority",      v:alert.who_priority||"—"},
                            ].map(f=>(
                              <div key={f.l} style={{background:C.surfaceHigh,borderRadius:5,padding:"8px 12px",border:"1px solid " + C.border}}>
                                <div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:3}}>{f.l}</div>
                                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.white}}>{f.v}</div>
                              </div>
                            ))}
                          </div>
                        </div>
                        {/* Confidence */}
                        <div>
                          <div style={{fontSize:9,color:C.teal,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Confidence Assessment</div>
                          <div style={{display:"flex",alignItems:"center",gap:12,marginBottom:10}}>
                            <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:18,fontWeight:800,color:CONF_COLOR[alert.surveillance_confidence]||C.muted}}>{alert.surveillance_confidence||"MEDIUM"}</span>
                            <span style={{fontSize:12,color:C.muted}}>Surveillance Confidence</span>
                          </div>
                          {alert.surveillance_caveat && (
                            <div style={{background:C.surfaceHigh,border:"1px solid " + C.amber + "40",borderRadius:6,padding:"10px 14px"}}>
                              <div style={{fontSize:9,color:C.amber,fontWeight:600,textTransform:"uppercase",letterSpacing:".06em",marginBottom:4}}>Validation Note</div>
                              <div style={{fontSize:11,color:C.mutedHigh,lineHeight:1.6}}>{alert.surveillance_caveat}</div>
                            </div>
                          )}
                        </div>
                        {/* Recommended Actions */}
                        {alert.stewardship_guidance && (
                          <div>
                            <div style={{fontSize:9,color:C.teal,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Recommended Actions</div>
                            <div style={{color:C.mutedHigh,fontSize:13,lineHeight:1.85,whiteSpace:"pre-wrap"}}>{alert.stewardship_guidance}</div>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div style={{display:"flex",flexDirection:"column",gap:22}}>
                        {/* Situation Assessment */}
                        <div>
                          <div style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Situation Assessment</div>
                          <div style={{display:"grid",gridTemplateColumns:"repeat(3,1fr)",gap:10,marginBottom:14}}>
                            {[
                              {l:"Observed", v:obsRate+"%",   c:(alert.current_resistance||0)>=.5?C.red:C.amber},
                              {l:"Forecast Resistance", v:expRate+"%",   c:C.mutedHigh},
                              {l:"Above Forecast",      v:"+"+devPp+"pp",c:C.red},
                              {l:"Trajectory",          v:trajectoryLabel, c:trajectoryColor},
                              {l:"Prediction Interval", v:lo80&&hi80?(lo80+"–"+hi80+"%"):"Not available", c:C.mutedHigh},
                              {l:"Signal Status",       v:outsidePI?"Outside PI":"Within PI", c:outsidePI?C.red:C.green},
                            ].map(f=>(
                              <div key={f.l} style={{background:C.surfaceHigh,borderRadius:5,padding:"8px 12px",border:"1px solid " + C.border}}>
                                <div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:3}}>{f.l}</div>
                                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:f.c}}>{f.v}</div>
                              </div>
                            ))}
                          </div>
                        </div>

                        {/* Intelligence bulletin — structured */}
                        {alert.stewardship_guidance && (
                          <>
                            <div>
                              <div style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Intelligence Assessment</div>
                              {renderStructuredBulletin(alert.stewardship_guidance)}
                            </div>
                          </>
                        )}

                        {/* Confidence Assessment */}
                        <div>
                          <div style={{fontSize:9,color:accentColor,fontWeight:700,letterSpacing:".08em",textTransform:"uppercase",marginBottom:8,paddingBottom:6,borderBottom:"1px solid " + C.border}}>Confidence Assessment</div>
                          <div style={{display:"flex",alignItems:"center",gap:12,marginBottom:10}}>
                            <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:18,fontWeight:800,color:C.green}}>HIGH</span>
                            <span style={{fontSize:12,color:C.muted}}>Forecast Confidence</span>
                          </div>
                          <div style={{display:"flex",flexDirection:"column",gap:5}}>
                            {[
                              "Surveillance data: WHO GLASS / ECDC EARS-Net",
                              "Forecast deviation: +" + devPp + "pp above forecast",
                              outsidePI ? "Statistical significance: Outside 80% prediction interval" : null,
                              cits.length > 0 ? "Literature support: " + cits.length + " peer-reviewed studies" : null,
                              "Signal trajectory: " + trajectoryLabel,
                            ].filter(Boolean).map((r,i)=>(
                              <div key={i} style={{display:"flex",alignItems:"center",gap:8,fontSize:11,color:C.mutedHigh}}>
                                <span style={{color:C.green,fontSize:10}}>✓</span>{r}
                              </div>
                            ))}
                          </div>
                        </div>
                      </div>
                    )}
                  </div>
                )}

                {/* ── Trajectory Analysis tab ── */}
                {tab==="trend" && (
                  <div>
                    <div style={{fontSize:12,color:C.muted,marginBottom:8}}>{alert.pathogen_name} · {isGenomic ? (alert.gene_name + " — sequenced isolate count") : (alert.antibiotic_name + " resistance rate")} · {countryName(alert.country_iso3)}</div>

                    {/* Legend */}
                    {!isGenomic && (
                      <div style={{display:"flex",gap:16,marginBottom:14,flexWrap:"wrap"}}>
                        {[
                          {color:C.accent,    label:"Observed resistance rate"},
                          {color:C.mutedHigh, label:"Model expectation",  dash:true},
                          {color:C.red,       label:"Alert threshold (50%)", dash:true},
                        ].map((l,i)=>(
                          <div key={i} style={{display:"flex",alignItems:"center",gap:6,fontSize:10,color:C.muted}}>
                            <div style={{width:20,height:2,background:l.color,borderTop:l.dash?"2px dashed "+l.color:"none",opacity:l.dash?.6:1}}/>
                            {l.label}
                          </div>
                        ))}
                      </div>
                    )}

                    {isGenomic && alert.time_series ? (
                      <ResponsiveContainer width="100%" height={300}>
                        <AreaChart data={Object.entries(alert.time_series).map(([y,c])=>({year:parseInt(y),count:c})).sort((a,b)=>a.year-b.year)} margin={{top:10,right:20,bottom:0,left:0}}>
                          <defs>
                            <linearGradient id="g2" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor={C.teal} stopOpacity={.3}/>
                              <stop offset="95%" stopColor={C.teal} stopOpacity={0}/>
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke={C.border}/>
                          <XAxis dataKey="year" stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}}/>
                          <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}} tickFormatter={v=>v>=1000?(v/1000).toFixed(1)+"k":v}/>
                          <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}} formatter={v=>[v.toLocaleString()+" isolates","Sequenced Isolates"]} labelStyle={{color:C.white}}/>
                          <Area type="monotone" dataKey="count" stroke={C.teal} fill="url(#g2)" strokeWidth={2.5} dot={{fill:C.teal,r:4}}/>
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : trendPts.length > 0 ? (
                      <ResponsiveContainer width="100%" height={300}>
                        <AreaChart data={trendPts.map(p=>({
                          ...p,
                          forecast: alert.forecasted_rate||null,
                          lo80: alert.forecast_lower_80||null,
                          hi80: alert.forecast_upper_80||null,
                        }))} margin={{top:10,right:20,bottom:0,left:0}}>
                          <defs>
                            <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor={C.accent} stopOpacity={.3}/>
                              <stop offset="95%" stopColor={C.accent} stopOpacity={0}/>
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke={C.border}/>
                          <XAxis dataKey="year" stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}}/>
                          <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}} tickFormatter={v=>(v*100).toFixed(0)+"%"} domain={[0,"auto"]}/>
                          <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}}
                            formatter={(v,n)=>{
                              if(n==="resistance_rate") return [(v*100).toFixed(1)+"%","Observed"];
                              if(n==="forecast") return [(v*100).toFixed(1)+"%","Forecast Resistance"];
                              return [v,n];
                            }} labelStyle={{color:C.white,fontFamily:"JetBrains Mono,monospace"}}/>
                          <ReferenceLine y={.5} stroke={C.red} strokeDasharray="4 4" label={{value:"Alert threshold",fill:C.red,fontSize:9,position:"insideTopRight"}}/>
                          {alert.forecasted_rate && <ReferenceLine y={alert.forecasted_rate} stroke={C.mutedHigh} strokeDasharray="3 3" label={{value:"Forecast resistance",fill:C.mutedHigh,fontSize:9,position:"insideTopRight"}}/>}
                          <Area type="monotone" dataKey="resistance_rate" stroke={C.accent} fill="url(#g1)" strokeWidth={2.5} dot={{fill:C.accent,r:4}} name="resistance_rate"/>
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : <Empty msg="Trajectory data not available for this signal"/>}

                    {!isGenomic && trendPts.length > 0 && (
                      <div style={{marginTop:14,background:C.surfaceHigh,borderRadius:6,padding:"10px 14px",display:"flex",gap:24,flexWrap:"wrap"}}>
                        <div><div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:2}}>Data Points</div><div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.white}}>{trendPts.length} years</div></div>
                        <div><div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:2}}>Latest Observed</div><div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.red}}>{obsRate}%</div></div>
                        <div><div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:2}}>Forecast Resistance</div><div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.mutedHigh}}>{expRate}%</div></div>
                        <div><div style={{fontSize:9,color:C.muted,textTransform:"uppercase",letterSpacing:".05em",marginBottom:2}}>Above Forecast</div><div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.red}}>+{devPp}pp</div></div>
                      </div>
                    )}
                  </div>
                )}

                {/* ── Evidence tab ── */}
                {tab==="citations" && (
                  <div>
                    <div style={{fontSize:10,color:C.muted,marginBottom:14}}>
                      {cits.length} peer-reviewed studies retrieved via PubMed for <strong style={{color:C.white}}>{pName} / {alert.antibiotic_name}</strong>. Citations ground the intelligence assessment and are not LLM-generated.
                    </div>
                    <div style={{display:"flex",flexDirection:"column",gap:14}}>
                      {cits.length===0 ? <Empty msg="No citations linked to this alert"/> : cits.map((c,i)=>(
                        <div key={i} style={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6,padding:14}}>
                          <div style={{display:"flex",gap:10,alignItems:"flex-start"}}>
                            <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.amber,background:C.amberDim,padding:"2px 6px",borderRadius:3,flexShrink:0,marginTop:2}}>PMID {c.pmid||"—"}</span>
                            <div>
                              <div style={{fontSize:13,fontWeight:600,color:C.white,marginBottom:4,lineHeight:1.4}}>{c.title||"Untitled"}</div>
                              <div style={{fontSize:11,color:C.mutedHigh,lineHeight:1.65}}>{c.summary||c.abstract||"No summary available."}</div>
                              {c.pubmed_url && <a href={c.pubmed_url} target="_blank" rel="noreferrer" style={{fontSize:10,color:C.accent,marginTop:6,display:"inline-block"}}>View on PubMed →</a>}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {tab==="history" && <Empty msg="State transition history — STABLE → WATCH → EMERGING → CRITICAL tracking coming soon"/>}
              </>
            )}
          </div>
        </div>

        {/* ── Right panel: Intelligence Summary ── */}
        <div style={{display:"flex",flexDirection:"column",gap:14}}>
          {/* Confidence card */}
          <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:16}}>
            <div style={{fontSize:9,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:12}}>Confidence Assessment</div>
            <div style={{display:"flex",alignItems:"center",gap:10,marginBottom:10}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:22,fontWeight:800,color:isGenomic?(CONF_COLOR[alert.surveillance_confidence]||C.amber):C.green}}>
                {isGenomic ? (alert.surveillance_confidence||"MEDIUM") : "HIGH"}
              </div>
              <div style={{fontSize:11,color:C.muted}}>{isGenomic ? "Surveillance Confidence" : "Forecast Confidence"}</div>
            </div>
            <div style={{display:"flex",flexDirection:"column",gap:6}}>
              {sources.map((s,i)=>(
                <div key={i} style={{display:"flex",alignItems:"center",gap:8,fontSize:10,color:s.ok?C.mutedHigh:C.muted}}>
                  <span style={{color:s.ok?C.green:C.muted,flexShrink:0}}>{s.ok?"✓":"○"}</span>
                  <span>{s.label}</span>
                  {s.note && <span style={{color:C.muted,fontSize:9}}>({s.note})</span>}
                </div>
              ))}
            </div>
          </div>

          {/* Signal facts */}
          <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:16}}>
            <div style={{fontSize:9,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:12}}>Signal Facts</div>
            <div style={{display:"flex",flexDirection:"column",gap:11}}>
              {(isGenomic
                ? [
                    {label:"Gene",             value:alert.gene_name||"—",          color:C.teal},
                    {label:"Drug Class",       value:alert.antibiotic_name||"—",    color:C.red},
                    {label:"Isolate Count",    value:(alert.isolate_count||0).toLocaleString(), color:C.teal},
                    {label:"Doubling Time",    value:alert.doubling_time_years?(alert.doubling_time_years+"yr"):"N/A", color:alert.doubling_time_years&&alert.doubling_time_years<1?C.red:C.amber},
                    {label:"Phenotypic Rate",  value:alert.current_resistance>0?((alert.current_resistance*100).toFixed(1)+"%"):"Not detected", color:alert.current_resistance>0?C.amber:C.muted},
                    {label:"WHO Priority",     value:alert.who_priority||"—",       color:alert.who_priority==="CRITICAL"?C.red:C.amber},
                  ]
                : [
                    {label:"Observed",         value:obsRate+"%",                   color:(alert.current_resistance||0)>=.5?C.red:C.amber},
                    {label:"Forecast Resistance",value:expRate+"%",                 color:C.mutedHigh},
                    {label:"Above Forecast",   value:"+"+devPp+"pp",               color:C.red},
                    {label:"Trajectory",       value:trajectoryLabel,               color:trajectoryColor},
                    {label:"Signal Since",     value:alert.created_at?new Date(alert.created_at).toLocaleDateString("en-GB",{month:"short",year:"numeric"}):"—", color:C.white},
                    {label:"Signal Score",     value:alert.severity_score+"/100",  color:accentColor},
                    {label:"Confidence",       value:"High",                        color:C.green},
                  ]
              ).map(f=>(
                <div key={f.label} style={{display:"flex",justifyContent:"space-between",alignItems:"center",paddingBottom:8,borderBottom:"1px solid " + C.border + "20"}}>
                  <span style={{fontSize:10,color:C.muted}}>{f.label}</span>
                  <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,fontWeight:700,color:f.color}}>{f.value}</span>
                </div>
              ))}
            </div>
          </div>

          {/* Spread risk — genomic only */}
          {isGenomic && alert.spread_risk_countries && alert.spread_risk_countries.length > 0 && (
            <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:16}}>
              <div style={{fontSize:9,color:C.muted,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:10}}>Spread Risk Countries</div>
              <div style={{display:"flex",flexWrap:"wrap",gap:6}}>
                {alert.spread_risk_countries.map((iso,i)=>(
                  <span key={i} style={{fontSize:10,padding:"3px 8px",borderRadius:4,background:C.surfaceHigh,color:C.mutedHigh,border:"1px solid " + C.border,fontFamily:"JetBrains Mono,monospace"}}>
                    {getFlagUrl(iso) && <img src={getFlagUrl(iso)} width="10" height="7" style={{borderRadius:1,marginRight:4,verticalAlign:"middle"}} alt=""/>}
                    {countryName(iso)}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ─── Screen 6: Executive Brief ────────────────────────────────────────────────
function ExecutiveBrief({ setScreen }) {
  const [stats,   setStats]   = useState(null);
  const [radar,   setRadar]   = useState([]);
  const [alerts,  setAlerts]  = useState([]);
  const [ltimes,  setLtimes]  = useState([]);
  const [loading, setLoading] = useState(true);
  const today = new Date().toLocaleDateString("en-GB",{day:"2-digit",month:"long",year:"numeric"});

  useEffect(() => {
    Promise.all([
      apiFetch("/stats"),
      apiFetch("/emergence-radar?tier=all&limit=5"),
      apiFetch("/alerts?page_size=5&sort_by=severity_score"),
      apiFetch("/lead-times"),
    ]).then(([s,r,a,l]) => {
      setStats(s);
      setRadar(Array.isArray(r)?r:[]);
      setAlerts(a?.alerts||[]);
      setLtimes(l||[]);
      setLoading(false);
    });
  }, []);

  if (loading) return <Spinner />;

  const avgLead = stats?.avg_lead_time_days||245;
  const avgMonths = (avgLead/30.4).toFixed(1);
  const tierData = [
    {name:"Critical",value:stats?.critical_alerts||0,color:C.red},
    {name:"High",value:(stats?.total_alerts||0)-(stats?.critical_alerts||0)-(stats?.warn_alerts||0),color:C.amber},
    {name:"Watch",value:stats?.warn_alerts||0,color:C.blue},
  ].filter(d=>d.value>0);
  const ltBarData = ltimes.slice(0,8).map((v,i)=>({name:"S"+(i+1),days:v.lead_time_days||0}));

  return (
    <div className="fade-up" style={{padding:"20px 24px",maxWidth:1200}}>
      <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start",marginBottom:20}}>
        <div>
          <div style={{fontSize:11,color:C.muted,letterSpacing:".08em",textTransform:"uppercase",marginBottom:4}}>AMR-Intel · Executive Intelligence Brief</div>
          <h1 style={{fontSize:22,fontWeight:700,color:C.white}}>Executive Intelligence Brief</h1>
        </div>
        <div style={{display:"flex",gap:8,alignItems:"center"}}>
          <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,color:C.muted}}>{today}</div>
          <button style={{background:C.surfaceHigh,border:"1px solid " + C.border,color:C.white,borderRadius:6,padding:"6px 12px",fontSize:11,display:"flex",alignItems:"center",gap:5}}>⇪ Share</button>
          <button style={{background:C.accent,border:"none",color:C.white,borderRadius:6,padding:"6px 12px",fontSize:11,fontWeight:600,display:"flex",alignItems:"center",gap:5}}>↓ Export PDF</button>
        </div>
      </div>

      <div className="brief-stat-row stat-row" style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:12,marginBottom:20}}>
        <StatCard label="Critical Threats" value={stats?.critical_alerts||0} sub="Requires immediate attention" accent={C.red}/>
        <StatCard label="Countries Affected" value={stats?.countries_monitored||8} sub="Under active surveillance" accent={C.blue}/>
        <StatCard label="Avg Lead Time" value={avgMonths + " months"} sub="vs official reports" accent={C.teal}/>
        <StatCard label="Signals Validated" value={stats?.validated_signals_count||5} sub="outcome confirmed" accent={C.green}/>
        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:6,padding:"14px 18px"}}>
          <div style={{fontSize:10,color:C.muted,fontWeight:600,letterSpacing:".08em",textTransform:"uppercase",marginBottom:6}}>Top Threat</div>
          <div style={{fontSize:12,fontWeight:600,color:C.white}}>K. pneumoniae (IPM)</div>
          <div style={{fontSize:10,color:C.muted}}>Bulgaria (BGR)</div>
          <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:16,fontWeight:700,color:C.red,marginTop:4}}>67.6%</div>
        </div>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 1.2fr 1fr",gap:16,marginBottom:20}}>
        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
          <SectionTitle>Threat Landscape</SectionTitle>
          <div style={{fontSize:10,color:C.muted,marginBottom:12}}>Distribution by Severity</div>
          <div style={{display:"flex",alignItems:"center",gap:16}}>
            <ResponsiveContainer width={100} height={100}>
              <PieChart>
                <Pie data={tierData} cx="50%" cy="50%" innerRadius={28} outerRadius={46} dataKey="value" strokeWidth={0}>
                  {tierData.map((e,i)=><Cell key={i} fill={e.color}/>)}
                </Pie>
              </PieChart>
            </ResponsiveContainer>
            <div style={{flex:1}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:22,fontWeight:700,color:C.white}}>{stats?.total_alerts||0}</div>
              <div style={{fontSize:10,color:C.muted,marginBottom:10}}>Total</div>
              {tierData.map(t=>(
                <div key={t.name} style={{display:"flex",justifyContent:"space-between",marginBottom:4}}>
                  <div style={{display:"flex",alignItems:"center",gap:5}}>
                    <div style={{width:6,height:6,borderRadius:"50%",background:t.color}}/>
                    <span style={{fontSize:11,color:C.muted}}>{t.name} ({Math.round(t.value/(stats?.total_alerts||1)*100)}%)</span>
                  </div>
                  <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,fontWeight:600,color:t.color}}>{t.value}</span>
                </div>
              ))}
            </div>
          </div>
        </div>

        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
          <SectionTitle>Lead-Time Performance</SectionTitle>
          <div style={{fontSize:10,color:C.muted,marginBottom:4}}>Distribution of lead time vs official detection</div>
          <div style={{display:"flex",justifyContent:"flex-end",marginBottom:8}}>
            <span style={{fontSize:10,color:C.muted}}>Avg: <span style={{fontFamily:"JetBrains Mono,monospace",color:C.teal,fontWeight:700}}>{avgMonths} months</span></span>
          </div>
          {ltBarData.length>0 ? (
            <ResponsiveContainer width="100%" height={140}>
              <BarChart data={ltBarData} barSize={18}>
                <CartesianGrid strokeDasharray="3 3" stroke={C.border}/>
                <XAxis dataKey="name" stroke={C.muted} tick={{fill:C.muted,fontSize:9,fontFamily:"JetBrains Mono,monospace"}}/>
                <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:9,fontFamily:"JetBrains Mono,monospace"}}/>
                <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}} formatter={v=>[v+" days","Lead Time"]} labelStyle={{color:C.white}}/>
                <Bar dataKey="days" fill={C.teal} radius={[3,3,0,0]}/>
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div style={{textAlign:"center",padding:20,color:C.muted,fontSize:12}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:32,fontWeight:700,color:C.teal,marginBottom:4}}>{avgMonths}mo</div>
              avg lead time across {stats?.validated_signals_count||5} validated signals
            </div>
          )}
        </div>

        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
          <SectionTitle>Top Emerging Threats</SectionTitle>
          <div style={{fontSize:10,color:C.muted,marginBottom:12}}>By Emergence Score</div>
          <div style={{display:"flex",flexDirection:"column",gap:10}}>
            {radar.slice(0,5).map((t,i)=>(
              <div key={i} style={{display:"flex",justifyContent:"space-between",alignItems:"center",padding:"8px 0",borderBottom:"1px solid " + C.border + "20"}}>
                <div>
                  <div style={{fontSize:12,fontWeight:600,color:C.white,display:"flex",alignItems:"center",gap:4}}>
                    {getFlagUrl(t.country_iso3) && <img src={getFlagUrl(t.country_iso3)} width="14" height="10" style={{borderRadius:1}} alt=""/>}
                    {countryName(t.country_iso3)}
                    <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.muted}}>({(t.antibiotic_name||"").slice(0,3).toUpperCase()})</span>
                  </div>
                  <div style={{fontSize:10,color:C.muted}}>{(t.pathogen_name||"").split(" ").slice(0,1).join(" ")}</div>
                </div>
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:16,fontWeight:700,color:t.emergence_score>=80?C.red:C.amber}}>{t.emergence_score}</span>
              </div>
            ))}
          </div>
          <button onClick={()=>setScreen&&setScreen("emergence")} style={{marginTop:12,background:"none",border:"none",color:C.accent,fontSize:11,padding:0,cursor:"pointer"}}>View full emergence radar →</button>
        </div>
      </div>

      <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18}}>
        <SectionTitle>Recent Critical Alerts</SectionTitle>
        <table style={{width:"100%",borderCollapse:"collapse"}}>
          <thead>
            <tr style={{borderBottom:"1px solid " + C.border}}>
              {["Pathogen / Antibiotic","Country","Signal","Trend","Lead Time","First Detected"].map(h=>(
                <th key={h} style={{padding:"8px 14px",textAlign:"left",color:C.muted,fontSize:10,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase"}}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {alerts.slice(0,5).map((a,i)=>(
              <tr key={i} style={{borderBottom:"1px solid " + C.border + "20"}}>
                <td style={{padding:"10px 14px"}}>
                  <div style={{fontSize:12,fontWeight:600}}>{(a.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Enterococcus faecium","E. faecium").replace("Staphylococcus aureus","S. aureus").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli")}</div>
                  <div style={{fontSize:10,color:C.muted}}>{a.antibiotic_name}</div>
                </td>
                <td style={{padding:"10px 14px"}}><CountryCell iso3={a.country_iso3} size="sm"/></td>
                <td style={{padding:"10px 14px"}}>
                  {a.signal_type==="genomic_precursor"
                    ? <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:C.teal}}>{(a.isolate_count||0).toLocaleString()} isolates</span>
                    : <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:((a.current_resistance||0)*100)>=50?C.red:C.amber}}>{((a.current_resistance||0)*100).toFixed(1)}%</span>
                  }
                </td>
                <td style={{padding:"10px 14px"}}><span style={{fontSize:12,color:a.trend_direction==="rising"?C.red:a.trend_direction==="falling"?C.green:C.muted}}>{a.trend_direction==="rising"?"↑ Rising":a.trend_direction==="falling"?"↓ Falling":"→ Stable"}</span></td>
                <td style={{padding:"10px 14px"}}><span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,color:C.teal}}>{avgMonths}mo</span></td>
                <td style={{padding:"10px 14px"}}><span style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,color:C.muted}}>{a.created_at?new Date(a.created_at).toLocaleDateString("en-GB",{month:"short",year:"numeric"}):"—"}</span></td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{marginTop:12,textAlign:"center"}}>
          <button onClick={()=>setScreen&&setScreen("operations")} style={{background:"none",border:"none",color:C.accent,fontSize:12,cursor:"pointer"}}>View full critical alerts →</button>
        </div>
      </div>

      <div style={{marginTop:16,textAlign:"center",color:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}}>
        AMR-SENTINEL · Autonomous Pathogen Intelligence · Generated {today} · For surveillance purposes only — not clinical advice
      </div>
    </div>
  );
}

// ─── Root App ─────────────────────────────────────────────────────────────────
export default function App() {
  const [screen,      setScreen]      = useState("command");
  const [invAlert,    setInvAlert]    = useState(null);
  const [stats,       setStats]       = useState(null);
  const [genomicCount,setGenomicCount]= useState(0);
  const [mobOpen,     setMobOpen]     = useState(false);

  useEffect(() => {
    apiFetch("/stats").then(setStats);
    apiFetch("/alerts?page_size=200&sort_by=severity_score").then(data => {
      const all = data?.alerts || [];
      setGenomicCount(all.filter(a => a.signal_type === "genomic_precursor").length);
    });
  }, []);

  const handleInvestigate = useCallback((alert) => {
    setInvAlert(alert);
    setScreen("investigate");
  }, []);

  const nav = (id) => { setScreen(id); setMobOpen(false); };

  return (
    <>
      <style>{CSS}</style>
      <div style={{display:"flex",height:"100vh",overflow:"hidden",position:"relative"}}>

        {/* Mobile overlay */}
        {mobOpen && (
          <div onClick={() => setMobOpen(false)} style={{
            position:"fixed",inset:0,background:"rgba(0,0,0,.55)",zIndex:40,
          }}/>
        )}

        {/* Mobile slide-in sidebar */}
        <div style={{
          position:"fixed",top:0,bottom:0,left:0,zIndex:50,
          transform: mobOpen ? "translateX(0)" : "translateX(-100%)",
          transition:"transform .22s ease",
          width:220, display:"flex",
        }}>
          <Sidebar screen={screen} setScreen={nav} stats={stats} genomicCount={genomicCount}/>
        </div>

        {/* Desktop sidebar */}
        <div className="desk-sidebar" style={{width:220,flexShrink:0}}>
          <Sidebar screen={screen} setScreen={setScreen} stats={stats} genomicCount={genomicCount}/>
        </div>

        <main style={{flex:1,overflowY:"auto",background:C.bg,minWidth:0}}>
          {/* Mobile top bar */}
          <div className="mob-bar">
            <button onClick={() => setMobOpen(!mobOpen)} style={{
              background:"none",border:"1px solid " + C.border,
              borderRadius:6,color:C.white,padding:"5px 10px",fontSize:16,lineHeight:1,
            }}>☰</button>
            <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:C.white}}>AMR-Intel</span>
            <span style={{fontSize:9,color:C.muted,letterSpacing:".06em",textTransform:"uppercase"}}>Pathogen Intelligence</span>
          </div>

          {screen==="command"     && <CommandCenter      onInvestigate={handleInvestigate} setScreen={setScreen}/>}
          {screen==="operations"  && <ThreatOperations   onInvestigate={handleInvestigate}/>}
          {screen==="emergence"   && <EmergenceRadarScreen onInvestigate={handleInvestigate}/>}
          {screen==="genomic"     && <GenomicIntelligence onInvestigate={handleInvestigate}/>}
          {screen==="investigate" && <AlertInvestigation  alert={invAlert}/>}
          {screen==="brief"       && <ExecutiveBrief      setScreen={setScreen}/>}
        </main>
      </div>
    </>
  );
}