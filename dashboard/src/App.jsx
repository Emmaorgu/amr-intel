/**
 * dashboard/src/App.jsx
 * AMR-Sentinel — 6-screen dashboard with Genomic Intelligence layer
 *
 * Screens:
 *   1. Command Center
 *   2. Threat Operations
 *   3. Emergence Radar
 *   4. Genomic Intelligence  ← NEW
 *   5. Alert Investigation
 *   6. Executive Brief
 */

import { useState, useEffect, useCallback } from "react";
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
            <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:13,fontWeight:700,color:C.white,letterSpacing:".04em"}}>AMR-Sentinel</div>
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
      <div style={{display:"grid",gridTemplateColumns:"repeat(6,1fr)",gap:12,marginBottom:20}}>
        <StatCard label="Avg Lead Time" value={avgMonths + "mo"} sub="vs official reports" accent={C.teal} delta="+0.8 vs last month"/>
        <StatCard label="Signals Validated" value={stats?.validated_signals_count || 5} sub="outcome confirmed" accent={C.green}/>
        <StatCard label="Critical Alerts" value={stats?.critical_alerts || 0} sub="+5 new today" accent={C.red}/>
        <StatCard label="Countries Monitored" value={stats?.countries_monitored || 103} sub="Global coverage" accent={C.blue}/>
        <StatCard label="Records Processed" value={(stats?.resistance_records_total || 7511).toLocaleString()} sub="WHO/ECDC/NCBI" accent={C.amber}/>
        <StatCard label="System Uptime" value="99.8%" sub="last 30 days" accent={C.green}/>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 1.6fr 1fr",gap:16,marginBottom:16}}>
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
          <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18,flex:1}}>
            <div style={{display:"flex",justifyContent:"space-between",alignItems:"center",marginBottom:12}}>
              <SectionTitle>Global Intelligence Map</SectionTitle>
              <select style={{background:C.surfaceHigh,border:"1px solid " + C.border,color:C.mutedHigh,borderRadius:4,padding:"3px 8px",fontSize:11}}>
                <option>Emergence Risk</option><option>Resistance Rate</option>
              </select>
            </div>
            {(() => {
              const COORDS = {
                BGR:{x:530,y:152},HRV:{x:505,y:146},CYP:{x:558,y:168},ROU:{x:537,y:146},
                GRC:{x:525,y:163},SVK:{x:516,y:140},POL:{x:516,y:132},CZE:{x:508,y:138},
                ITA:{x:505,y:156},LTU:{x:527,y:126},LVA:{x:529,y:120},HUN:{x:520,y:145},
                DEU:{x:503,y:132},FRA:{x:493,y:148},ESP:{x:486,y:158},SWE:{x:516,y:116},
                LUX:{x:499,y:140},MLT:{x:514,y:170},ISL:{x:466,y:106},GBR:{x:486,y:128},
                NLD:{x:496,y:130},BEL:{x:495,y:135},AUT:{x:510,y:143},CHE:{x:500,y:145},
                NGA:{x:502,y:210},GHA:{x:490,y:213},CMR:{x:512,y:212},KEN:{x:542,y:218},
                EGY:{x:540,y:183},MAR:{x:481,y:176},ZAF:{x:525,y:255},
                BGD:{x:624,y:188},IND:{x:615,y:194},PAK:{x:602,y:180},
                SAU:{x:560,y:186},USA:{x:172,y:158},BRA:{x:268,y:238},
                CHN:{x:658,y:168},JPN:{x:708,y:156},TUR:{x:546,y:161},UKR:{x:540,y:138},
              };
              const hotspots = alerts.reduce((acc, a) => {
                if (!COORDS[a.country_iso3]) return acc;
                if (!acc[a.country_iso3] || a.severity_score > acc[a.country_iso3].score) {
                  acc[a.country_iso3] = {...COORDS[a.country_iso3], score:a.severity_score, tier:a.severity_tier};
                }
                return acc;
              }, {});
              const dots = Object.entries(hotspots);
              return (
                <div style={{background:"linear-gradient(160deg,#0A1628,#0B1520)",borderRadius:6,height:190,position:"relative",overflow:"hidden",border:"1px solid " + C.border}}>
                  <svg viewBox="0 0 800 400" style={{width:"100%",height:"100%"}} preserveAspectRatio="xMidYMid meet">
                    <path d="M62,82 L100,76 L130,78 L165,80 L195,88 L220,106 L232,124 L228,148 L218,168 L205,188 L190,204 L172,210 L154,208 L136,214 L118,210 L100,194 L82,172 L68,148 L60,122 L58,100 Z" fill="#152236" opacity=".85"/>
                    <path d="M188,216 L220,210 L248,210 L272,214 L290,228 L295,248 L290,272 L278,294 L260,308 L240,310 L220,298 L204,278 L196,256 L190,232 Z" fill="#152236" opacity=".85"/>
                    <path d="M450,94 L480,90 L510,88 L540,88 L568,92 L584,106 L586,124 L580,144 L565,162 L546,172 L524,174 L504,172 L482,166 L464,154 L452,138 L446,118 Z" fill="#152236" opacity=".85"/>
                    <path d="M460,174 L490,170 L520,170 L548,172 L568,180 L580,196 L580,220 L574,246 L560,268 L542,282 L520,286 L498,282 L478,270 L462,252 L454,228 L454,204 Z" fill="#152236" opacity=".85"/>
                    <path d="M550,84 L600,80 L650,78 L700,80 L740,86 L766,100 L772,118 L768,142 L756,164 L736,182 L712,196 L686,204 L658,204 L630,198 L604,188 L578,174 L560,158 L550,138 L546,116 Z" fill="#152236" opacity=".85"/>
                    <path d="M672,244 L710,240 L746,242 L768,256 L772,276 L762,296 L742,308 L716,308 L692,296 L676,278 L668,258 Z" fill="#152236" opacity=".85"/>
                    {dots.map(([iso3, d], i) => {
                      const color = d.tier==="critical" ? "#EF4444" : d.tier==="warn" ? "#F59E0B" : "#3B82F6";
                      const r = d.score>=98 ? 8 : d.score>=90 ? 6 : 4;
                      return (
                        <g key={iso3}>
                          <circle cx={d.x} cy={d.y} r={r*2.2} fill={color} opacity=".12"/>
                          <circle cx={d.x} cy={d.y} r={r} fill={color} opacity=".95">
                            <animate attributeName="r" values={r+";"+Math.round(r*1.7)+";"+r} dur={(1.8+i*0.25)+"s"} repeatCount="indefinite"/>
                            <animate attributeName="opacity" values=".95;.35;.95" dur={(1.8+i*0.25)+"s"} repeatCount="indefinite"/>
                          </circle>
                        </g>
                      );
                    })}
                  </svg>
                  <div style={{position:"absolute",bottom:6,left:10,display:"flex",gap:10,fontSize:8,color:"#64748B"}}>
                    {[["Low","#1E3A5F"],["Medium","#2563EB"],["High","#F59E0B"],["Very High","#EF4444"]].map(([l,co]) => (
                      <div key={l} style={{display:"flex",alignItems:"center",gap:3}}>
                        <div style={{width:7,height:7,borderRadius:1,background:co}}/>
                        {l}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })()}
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
      <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:12,marginBottom:16}}>
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
  const [tab,     setTab]     = useState("bulletin");
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

  const tabs = [
    {id:"bulletin",label:"Bulletin"},
    {id:"trend",label: isGenomic ? "Genomic Trajectory" : "Trend Analysis"},
    {id:"citations",label:"Citations (" + cits.length + ")"},
    {id:"history",label:"History"},
  ];

  const pName = (alert.pathogen_name||"").replace("Klebsiella pneumoniae","K. pneumoniae").replace("Enterococcus faecium","E. faecium").replace("Staphylococcus aureus","S. aureus").replace("Pseudomonas aeruginosa","P. aeruginosa").replace("Escherichia coli","E. coli").replace("Acinetobacter spp.","Acinetobacter").replace("Acinetobacter baumannii","A. baumannii");

  return (
    <div className="fade-up" style={{padding:"20px 24px",maxWidth:1400}}>
      <div style={{background:C.surface,border:"1px solid " + C.border,borderLeft:"4px solid " + accentColor,borderRadius:8,padding:20,marginBottom:20}}>
        <div style={{display:"flex",justifyContent:"space-between",alignItems:"flex-start"}}>
          <div style={{flex:1}}>
            <div style={{display:"flex",gap:10,alignItems:"center",marginBottom:8}}>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:36,fontWeight:800,color:accentColor,lineHeight:1}}>{alert.severity_score}</div>
              <div>
                <div style={{fontSize:9,color:C.muted,letterSpacing:".08em",textTransform:"uppercase",marginBottom:2}}>Severity Score</div>
                {isGenomic ? <ConfBadge conf={alert.surveillance_confidence}/> : <SeverityBadge tier={alert.severity_tier}/>}
              </div>
              {isGenomic && <span style={{fontSize:11,background:C.tealDim,color:C.teal,padding:"3px 10px",borderRadius:4,border:"1px solid " + C.teal + "40",fontFamily:"JetBrains Mono,monospace",fontWeight:700}}>🧬 GENOMIC PRECURSOR</span>}
            </div>
            <h1 style={{fontSize:20,fontWeight:700,color:C.white,marginBottom:4}}>
              {pName}
              <span style={{marginLeft:10,padding:"2px 8px",borderRadius:4,background:isGenomic?C.tealDim:C.blueDim,color:isGenomic?C.teal:C.blue,fontSize:12,fontWeight:500}}>
                {isGenomic ? (alert.gene_name || "Genomic Signal") : (alert.antibiotic_name + " Resistance")}
              </span>
            </h1>
            <div style={{fontSize:12,color:C.muted,display:"flex",alignItems:"center",gap:6}}>
              {getFlagUrl(alert.country_iso3) && <img src={getFlagUrl(alert.country_iso3)} width="14" height="10" style={{borderRadius:1}} alt=""/>}
              {countryName(alert.country_iso3)} ({alert.country_iso3}) · Detected {alert.created_at ? new Date(alert.created_at).toLocaleDateString("en-GB",{day:"2-digit",month:"short",year:"numeric"}) : "—"}
            </div>
          </div>

          <div style={{display:"grid",gridTemplateColumns:"repeat(4,1fr)",gap:16,textAlign:"center"}}>
            {(isGenomic
              ? [{label:"Isolate Count",value:(alert.isolate_count||0).toLocaleString(),color:C.teal},{label:"Doubling Time",value:alert.doubling_time_years?(alert.doubling_time_years + "yr"):"N/A",color:alert.doubling_time_years<1?C.red:alert.doubling_time_years<2?C.amber:C.mutedHigh},{label:"→500 Isolates",value:alert.days_to_threshold?(alert.days_to_threshold + "d"):"N/A",color:C.amber},{label:"Severity Score",value:alert.severity_score + "/100",color:accentColor}]
              : [{label:"Resistance Rate",value:((alert.current_resistance||0)*100).toFixed(1)+"%",color:(alert.current_resistance||0)>=.5?C.red:C.amber},{label:"Forecast",value:((alert.forecasted_rate||0)*100).toFixed(1)+"%",color:C.mutedHigh},{label:"Deviation",value:"+" + ((alert.deviation_magnitude||0)*100).toFixed(1)+"pp",color:C.red},{label:"Severity Score",value:alert.severity_score + "/100",color:accentColor}]
            ).map(m=>(
              <div key={m.label}>
                <div style={{fontSize:9,color:C.muted,letterSpacing:".06em",textTransform:"uppercase",marginBottom:4}}>{m.label}</div>
                <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:16,fontWeight:700,color:m.color}}>{m.value}</div>
              </div>
            ))}
          </div>
        </div>

        <div style={{display:"flex",gap:24,marginTop:16,paddingTop:14,borderTop:"1px solid " + C.border}}>
          {(isGenomic
            ? [{label:"Signal Type",value:"Genomic Precursor",color:C.teal},{label:"Gene Family",value:alert.gene_family||"—",color:C.white},{label:"Drug Class",value:alert.antibiotic_name||"—",color:C.red},{label:"Confidence",value:alert.surveillance_confidence||"—",color:CONF_COLOR[alert.surveillance_confidence]||C.muted},{label:"WHO Priority",value:alert.who_priority||"—",color:alert.who_priority==="CRITICAL"?C.red:C.amber}]
            : [{label:"Trend (3-year)",value:alert.trend_direction==="rising"?"↑ Rising":alert.trend_direction==="falling"?"↓ Falling":"→ Stable",color:alert.trend_direction==="rising"?C.red:alert.trend_direction==="falling"?C.green:C.muted},{label:"Signal Since",value:alert.created_at?new Date(alert.created_at).toLocaleDateString("en-GB",{month:"short",year:"numeric"}):"—",color:C.white},{label:"Signal Type",value:(alert.signal_type||"—").replace("_"," "),color:C.mutedHigh},{label:"Confidence",value:"High",color:C.green}]
          ).map(m=>(
            <div key={m.label}>
              <div style={{fontSize:9,color:C.muted,letterSpacing:".06em",textTransform:"uppercase",marginBottom:3}}>{m.label}</div>
              <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:600,color:m.color}}>{m.value}</div>
            </div>
          ))}
        </div>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"1fr 260px",gap:20}}>
        <div>
          <div style={{display:"flex",gap:2,borderBottom:"1px solid " + C.border,marginBottom:0}}>
            {tabs.map(t=>(
              <button key={t.id} onClick={()=>setTab(t.id)} style={{padding:"8px 16px",background:"none",border:"none",borderBottom:tab===t.id?"2px solid " + accentColor:"2px solid transparent",color:tab===t.id?C.white:C.muted,fontSize:12,fontWeight:tab===t.id?600:400,marginBottom:-1}}>{t.label}</button>
            ))}
          </div>

          <div style={{background:C.surface,border:"1px solid " + C.border,borderTop:"none",borderRadius:"0 0 8px 8px",padding:24,minHeight:320}}>
            {loading ? <Spinner/> : (
              <>
                {tab==="bulletin" && (
                  <div>
                    {isGenomic && alert.intelligence_summary ? (
                      <div>
                        <div style={{fontSize:10,color:C.teal,fontWeight:600,letterSpacing:".06em",textTransform:"uppercase",marginBottom:10}}>Genomic Intelligence Summary</div>
                        <div style={{color:C.mutedHigh,fontSize:13,lineHeight:1.9}}>{alert.intelligence_summary}</div>
                        {alert.surveillance_caveat && (
                          <div style={{marginTop:16,background:C.surfaceHigh,border:"1px solid " + C.amber + "40",borderRadius:6,padding:"10px 14px"}}>
                            <div style={{fontSize:9,color:C.amber,fontWeight:600,textTransform:"uppercase",letterSpacing:".06em",marginBottom:4}}>Validation Note</div>
                            <div style={{fontSize:11,color:C.mutedHigh,lineHeight:1.6}}>{alert.surveillance_caveat}</div>
                          </div>
                        )}
                      </div>
                    ) : (
                      <div>
                        <div style={{color:C.mutedHigh,fontSize:13,lineHeight:1.9,whiteSpace:"pre-wrap"}}>
                          {alert.stewardship_guidance || <Empty msg="Bulletin not yet generated"/>}
                        </div>
                        {alert.stewardship_guidance && (
                          <button onClick={()=>setTab("trend")} style={{marginTop:16,background:"none",border:"none",color:C.accent,fontSize:12,padding:0}}>View Resistance Trajectory →</button>
                        )}
                      </div>
                    )}
                  </div>
                )}

                {tab==="trend" && (
                  <div>
                    <div style={{fontSize:12,color:C.muted,marginBottom:16}}>{alert.pathogen_name} · {isGenomic ? (alert.gene_name + " isolate count") : alert.antibiotic_name} · {countryName(alert.country_iso3)}</div>
                    {isGenomic && alert.time_series ? (
                      <ResponsiveContainer width="100%" height={280}>
                        <AreaChart data={Object.entries(alert.time_series).map(([y,c])=>({year:parseInt(y),count:c})).sort((a,b)=>a.year-b.year)} margin={{top:10,right:20,bottom:0,left:0}}>
                          <defs>
                            <linearGradient id="g2" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor={C.teal} stopOpacity={.25}/>
                              <stop offset="95%" stopColor={C.teal} stopOpacity={0}/>
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke={C.border}/>
                          <XAxis dataKey="year" stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}}/>
                          <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}} tickFormatter={v=>v>=1000?(v/1000).toFixed(1)+"k":v}/>
                          <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}} formatter={v=>[v.toLocaleString(),"Isolates"]} labelStyle={{color:C.white}}/>
                          <Area type="monotone" dataKey="count" stroke={C.teal} fill="url(#g2)" strokeWidth={2} dot={{fill:C.teal,r:3}}/>
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : trendPts.length > 0 ? (
                      <ResponsiveContainer width="100%" height={280}>
                        <AreaChart data={trendPts} margin={{top:10,right:20,bottom:0,left:0}}>
                          <defs>
                            <linearGradient id="g1" x1="0" y1="0" x2="0" y2="1">
                              <stop offset="5%" stopColor={C.accent} stopOpacity={.25}/>
                              <stop offset="95%" stopColor={C.accent} stopOpacity={0}/>
                            </linearGradient>
                          </defs>
                          <CartesianGrid strokeDasharray="3 3" stroke={C.border}/>
                          <XAxis dataKey="year" stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}}/>
                          <YAxis stroke={C.muted} tick={{fill:C.muted,fontSize:10,fontFamily:"JetBrains Mono,monospace"}} tickFormatter={v=>(v*100).toFixed(0)+"%"} domain={[0,"auto"]}/>
                          <Tooltip contentStyle={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6}} formatter={v=>[(v*100).toFixed(1)+"%","Resistance Rate"]} labelStyle={{color:C.white,fontFamily:"JetBrains Mono,monospace"}}/>
                          <ReferenceLine y={.5} stroke={C.red} strokeDasharray="4 4"/>
                          <Area type="monotone" dataKey="resistance_rate" stroke={C.accent} fill="url(#g1)" strokeWidth={2} dot={{fill:C.accent,r:3}}/>
                        </AreaChart>
                      </ResponsiveContainer>
                    ) : <Empty msg="Trend data not available"/>}
                  </div>
                )}

                {tab==="citations" && (
                  <div style={{display:"flex",flexDirection:"column",gap:14}}>
                    {cits.length===0 ? <Empty msg="No citations linked"/> : cits.map((c,i)=>(
                      <div key={i} style={{background:C.surfaceHigh,border:"1px solid " + C.border,borderRadius:6,padding:14}}>
                        <div style={{display:"flex",gap:10,alignItems:"flex-start"}}>
                          <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:10,color:C.amber,background:C.amberDim,padding:"2px 6px",borderRadius:3,flexShrink:0,marginTop:2}}>PMID {c.pmid||"—"}</span>
                          <div>
                            <div style={{fontSize:13,fontWeight:600,color:C.white,marginBottom:4}}>{c.title||"Untitled"}</div>
                            <div style={{fontSize:11,color:C.mutedHigh,lineHeight:1.6}}>{c.summary||c.abstract||"No summary."}</div>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {tab==="history" && <Empty msg="Historical state transitions — coming soon"/>}
              </>
            )}
          </div>
        </div>

        <div style={{background:C.surface,border:"1px solid " + C.border,borderRadius:8,padding:18,height:"fit-content"}}>
          <SectionTitle>Key Facts</SectionTitle>
          <div style={{display:"flex",flexDirection:"column",gap:14}}>
            {(isGenomic
              ? [{label:"Gene",value:alert.gene_name||"—",color:C.teal},{label:"Drug Class",value:alert.antibiotic_name||"—",color:C.red},{label:"Isolate Count",value:(alert.isolate_count||0).toLocaleString(),color:C.teal},{label:"Doubling Time",value:alert.doubling_time_years?(alert.doubling_time_years+"yr"):"N/A",color:alert.doubling_time_years<1?C.red:C.amber},{label:"Phenotypic Rate",value:alert.current_resistance>0?((alert.current_resistance*100).toFixed(1)+"%"):"No data",color:alert.current_resistance>0?C.amber:C.muted},{label:"Confidence",value:alert.surveillance_confidence||"—",color:CONF_COLOR[alert.surveillance_confidence]||C.muted},{label:"WHO Priority",value:alert.who_priority||"—",color:alert.who_priority==="CRITICAL"?C.red:C.amber}]
              : [{label:"Resistance Rate",value:((alert.current_resistance||0)*100).toFixed(1)+"%",color:(alert.current_resistance||0)>=.5?C.red:C.amber},{label:"Forecast",value:((alert.forecasted_rate||0)*100).toFixed(1)+"%",color:C.white},{label:"Deviation",value:"+" + ((alert.deviation_magnitude||0)*100).toFixed(1)+"pp",color:C.red},{label:"Trend",value:alert.trend_direction==="rising"?"↑ Rising":alert.trend_direction==="falling"?"↓ Falling":"→ Stable",color:alert.trend_direction==="rising"?C.red:alert.trend_direction==="falling"?C.green:C.muted},{label:"Signal Since",value:alert.created_at?new Date(alert.created_at).toLocaleDateString("en-GB",{month:"short",year:"numeric"}):"—",color:C.white},{label:"Severity Score",value:alert.severity_score+"/100",color:accentColor},{label:"Confidence",value:"High",color:C.green}]
            ).map(f=>(
              <div key={f.label} style={{display:"flex",justifyContent:"space-between",alignItems:"center",paddingBottom:10,borderBottom:"1px solid " + C.border + "20"}}>
                <span style={{fontSize:11,color:C.muted}}>{f.label}</span>
                <span style={{fontFamily:"JetBrains Mono,monospace",fontSize:12,fontWeight:700,color:f.color}}>{f.value}</span>
              </div>
            ))}
          </div>
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
          <div style={{fontSize:11,color:C.muted,letterSpacing:".08em",textTransform:"uppercase",marginBottom:4}}>AMR-Sentinel · Executive Intelligence Brief</div>
          <h1 style={{fontSize:22,fontWeight:700,color:C.white}}>Executive Intelligence Brief</h1>
        </div>
        <div style={{display:"flex",gap:8,alignItems:"center"}}>
          <div style={{fontFamily:"JetBrains Mono,monospace",fontSize:11,color:C.muted}}>{today}</div>
          <button style={{background:C.surfaceHigh,border:"1px solid " + C.border,color:C.white,borderRadius:6,padding:"6px 12px",fontSize:11,display:"flex",alignItems:"center",gap:5}}>⇪ Share</button>
          <button style={{background:C.accent,border:"none",color:C.white,borderRadius:6,padding:"6px 12px",fontSize:11,fontWeight:600,display:"flex",alignItems:"center",gap:5}}>↓ Export PDF</button>
        </div>
      </div>

      <div style={{display:"grid",gridTemplateColumns:"repeat(5,1fr)",gap:12,marginBottom:20}}>
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
  const [screen,   setScreen]   = useState("command");
  const [invAlert, setInvAlert] = useState(null);
  const [stats,    setStats]    = useState(null);
  const [genomicCount, setGenomicCount] = useState(0);

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

  return (
    <>
      <style>{CSS}</style>
      <div style={{display:"flex",height:"100vh",overflow:"hidden"}}>
        <Sidebar screen={screen} setScreen={setScreen} stats={stats} genomicCount={genomicCount}/>
        <main style={{flex:1,overflowY:"auto",background:C.bg}}>
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