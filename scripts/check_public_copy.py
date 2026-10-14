#!/usr/bin/env python3
"""Public-copy rule 2 (AGENTS.md): no country, city, region, nationality or flag.

Also rejects region-tagged locale literals (``xx-XX``) and place-named
timezones. Stdlib only, so the git hooks can run it without the venv.

    check_public_copy.py --tree          every tracked text file
    check_public_copy.py --staged        lines added in the index (pre-commit)
    check_public_copy.py --message FILE  a commit message (commit-msg)
    check_public_copy.py --history       every commit message on HEAD

The name lists are rot13 so the repo itself never spells a place out;
``codecs.encode(text, "rot13")`` turns a new entry into the stored form.
Language names (Spanish, English, ...) are allowed: Emma speaks languages,
not origins.
"""

from __future__ import annotations

import codecs
import re
import subprocess
import sys
from pathlib import Path

# Whole words, any case. "_" joins multi-word names.
_WORDS = (
    r"Nstunavfgna Nstnavfgáa Nyonavn Nytrevn Netryvn Naqbeen Natbyn Netragvan Nezravn "
    r"Nhfgenyvn Nhfgevn Nmreonvwna Nmreonvláa Onuenva Oneéva Onatynqrfu Oneonqbf "
    r"Orynehf Ovrybeehfvn Orytvhz Oéytvpn Oryvmr Oryvpr Orava Oraía Ouhgna Ohgáa "
    r"Obyvivn Obgfjnan Oenmvy Oenfvy Oeharv Ohytnevn Ohehaqv Pnzobqvn Pnzobln Pnzrebba "
    r"Pnzreúa Pnanqn Pnanqá Pbybzovn Pbzbebf Pbfgn_Evpn Pebngvn Pebnpvn Plcehf Puvcer "
    r"Pmrpuvn Purdhvn Qraznex Qvanznepn Qwvobhgv Qbzvavpna_Erchoyvp "
    r"Ercúoyvpn_Qbzvavpnan Rphnqbe Rtlcg Rtvcgb Ry_Fnyinqbe Revgern Rfgbavn Rfjngvav "
    r"Rguvbcvn Rgvbcín Svwv Svaynaq Svaynaqvn Senapr Senapvn Tnoba Tnoóa Tunan Terrpr "
    r"Terpvn Thngrznyn Thlnan Unvgv Unvgí Ubaqhenf Uhatnel Uhateín Vprynaq Vfynaqvn "
    r"Vaqbarfvn Vena Veáa Vend Venx Verynaq Veynaqn Vgnyl Vgnyvn Wnznvpn Wncna Wncóa "
    r"Wbeqnavn Xnmnxufgna Xraln Xravn Xvevongv Xbfbib Xhjnvg Xletlmfgna Yngivn Yrgbavn "
    r"Yronaba Yíonab Yrfbgub Yvorevn Yvoln Yvovn Yvrpugrafgrva Yvguhnavn Yvghnavn "
    r"Yhkrzobhet Yhkrzohetb Znqntnfpne Znynjv Znynlfvn Znynfvn Znyqvirf Znyqvinf "
    r"Znhevgnavn Znhevgvhf Zrkvpb Zékvpb Zéwvpb Zvpebarfvn Zbyqbin Zbyqnivn Zbanpb "
    r"Zóanpb Zbatbyvn Zbagrarteb Zbebppb Zneehrpbf Zbmnzovdhr Zlnazne Anzvovn Anheh "
    r"Arcny Argureynaqf Ubynaqn Cnífrf_Onwbf Arj_Mrnynaq Ahrin_Mrynaqn Avpnenthn "
    r"Avtrevn Abegu_Xbern Pbern_qry_Abegr Fbhgu_Xbern Pbern_qry_Fhe Abejnl Abehrtn "
    r"Bzáa Cnxvfgna Cnxvfgáa Cnynh Cnyrfgvar Cnyrfgvan Cnanzá Cnenthnl Creú "
    r"Cuvyvccvarf Svyvcvanf Cbynaq Cbybavn Cbeghtny Dngne Pngne Ebznavn Ehznavn "
    r"Ehznaín Ehffvn Ehfvn Ejnaqn Ehnaqn Fna_Znevab Fnhqv_Nenovn Nenovn_Fnhqvgn "
    r"Frartny Freovn Frlpuryyrf Fvreen_Yrbar Fvatncber Fvatnche Fybinxvn Rfybindhvn "
    r"Fybiravn Rfybiravn Fbznyvn Fbhgu_Nsevpn Fhqásevpn Fcnva Rfcnñn Fev_Ynaxn Fhqna "
    r"Fhqáa Fhevanzr Fhevanz Fjrqra Fhrpvn Fjvgmreynaq Fhvmn Flevn Fvevn Gnvjna Gnvjáa "
    r"Gnwvxvfgna Gnamnavn Gunvynaq Gnvynaqvn Ghavfvn Gúarm Ghexzravfgna Ghinyh Htnaqn "
    r"Hxenvar Hpenavn Havgrq_Neno_Rzvengrf Havgrq_Xvatqbz Ervab_Havqb Terng_Oevgnva "
    r"Tena_Oergnñn Ratynaq Vatyngreen Fpbgynaq Rfpbpvn Havgrq_Fgngrf Rfgnqbf_Havqbf "
    r"  RR\._?HH H\.F\.N\. Hehthnl Hmorxvfgna Inahngh Ingvpna Ingvpnab "
    r"Irarmhryn Ivrganz Lrzra Mnzovn Mvzonojr Chregb_Evpb Terraynaq Tebraynaqvn "
    r"Ubat_Xbat Znpnb Znpnh Gvorg Pngnybavn Pngnyhñn Pngnyhaln Yngva_Nzrevpn "
    r"Yngvabnzéevpn Yngvabnzrevpn Nzéevpn_Yngvan Vorebnzéevpn Uvfcnabnzéevpn YNGNZ "
    r"Abegu_Nzrevpn Abegrnzéevpn Fbhgu_Nzrevpn Fhqnzéevpn Fhenzéevpn Prageny_Nzrevpn "
    r"Pragebnzéevpn Pnevoorna Pnevor Rhebcr Rhebcn Nsevpn Ásevpn Fpnaqvanivn "
    r"Rfpnaqvanivn Onyxnaf Onypnarf Zvqqyr_Rnfg Zrqvb_Bevragr Bevragr_Zrqvb Cngntbavn "
    r"Naqnyhpín Naqnyhfvn Zbagreerl Thnqnynwnen PQZK Pvhqnq_qr_Zékvpb Zrkvpb_Pvgl "
    r"Ahrib_Yróa Ahrib_Yrba Onwn_Pnyvsbeavn Fna_Crqeb_Tnemn_Tnepín Tnemn_Tnepín "
    r"Ncbqnpn Rfpborqb Thnqnyhcr_AY Fnagn_Pngnevan Fna_Avpbyáf Ohrabf_Nverf Obtbgá "
    r"Obtbgn Zrqryyía Zrqryyva Pnyv Pnegntran Oneenadhvyyn Pnenpnf Znenpnvob Dhvgb "
    r"Thnlndhvy Zbagrivqrb Nfhapvóa Nfhapvba Yn_Cnm Fhper Fnagn_Pehm Fna_Wbfé Znanthn "
    r"Grthpvtnycn Fna_Fnyinqbe Pvhqnq_qr_Thngrznyn Uninan Yn_Unonan Fnagb_Qbzvatb "
    r"Fna_Whna Xvatfgba Ebfnevb Zraqbmn Inycnenífb Znqevq Onepryban Frivyyn Frivyyr "
    r"Ovyonb Mnentbmn Záyntn Znyntn Yvfoba Yvfobn Cbegb Bcbegb Ybaqba Ybaqerf Oreyva "
    r"Oreyía Zhavpu Zúavpu Unzohet Senaxsheg Ivraan Ivran Centhr Centn Jnefnj Inefbivn "
    r"Ohqncrfg Nzfgreqnz Oehffryf Oehfrynf Mhevpu Múevpu Trarin Tvaroen Zvyna Zvyáa "
    r"Ancyrf Aácbyrf Iravpr Irarpvn Nguraf Ngranf Vfgnaohy Rfgnzohy Zbfpbj Zbfpú Xlvi "
    r"Xvri Qhoyva Qhoyía Rqvaohetu Rqvzohetb Znapurfgre Yvirecbby Pbcrauntra "
    r"Pbcraunthr Fgbpxubyz Rfgbpbyzb Bfyb Uryfvaxv Gbxlb Gbxvb Bfnxn Xlbgb Frbhy Frúy "
    r"Orvwvat Crxía Funatunv Funatuáv Furamura Zhzonv Obzonl Qryuv Onatnyber Oratnyheh "
    r"Xnenpuv Qunxn Onatxbx Wnxnegn Lnxnegn Znavyn Unabv Qhonv Qhoáv Qbun Evlnqu Evnq "
    r"Gruena Grureáa Ontuqnq Ontqnq Wrehfnyrz Wrehfnyéa Gry_Nivi Pnveb Ry_Pnveb Yntbf "
    r"Anvebov Wbunaarfohet Wbunaarfohetb Pncr_Gbja Pnfnoynapn Zneenxrpu Gbebagb "
    r"Zbagerny Inapbhire Bggnjn Arj_Lbex Ahrin_Lbex ALP Ybf_Natryrf Ybf_Áatryrf "
    r"Fna_Senapvfpb Puvpntb Frnggyr Cuvynqrycuvn Svynqrysvn Fna_Qvrtb Fna_Nagbavb "
    r"Fvyvpba_Inyyrl Oebbxyla Znaunggna Zryobhear Nhpxynaq Fãb_Cnhyb Fnb_Cnhyb "
    r"Eíb_qr_Wnarveb Evb_qr_Wnarveb Oenfíyvn Oenfvyvn Fnagvntb_qr_Puvyr Zrkvpna "
    r"Zrkvpnaf zrkvpna[bn]f? Netragva(?:r|vna)f? netragva[bn]f? Pbybzovnaf? "
    r"pbybzovna[bn]f? Puvyrnaf? puvyra[bn]f? Crehivnaf? crehna[bn]f? Irarmhrynaf? "
    r"irarmbyna[bn]f? Phonaf? phona[bn]f? Rphnqbevnaf? rphngbevna[bn]f? Obyvivnaf? "
    r"obyvivna[bn]f? Hehthnlnaf? hehthnl[bn]f? Cnenthnlnaf? cnenthnl[bn]f? "
    r"Thngrznynaf? thngrznygrp[bn]f? Ubaqhenaf? ubaqherñ[bn]f? Fnyinqbenaf? "
    r"fnyinqberñ[bn]f? Avpnenthnaf? avpnentüraf(?:r|rf) Pbfgn_Evpnaf? "
    r"pbfgneevpraf(?:r|rf) Cnanznavnaf? cnanzrñ[bn]f? Qbzvavpnaf? Chregb_Evpnaf? "
    r"chregbeevdhrñ[bn]f? obevphnf? Oenmvyvnaf? oenfvyrñ[bn]f? Nzrevpnaf? "
    r"rfgnqbhavqraf(?:r|rf) abegrnzrevpna[bn]f? tevatbf? tevatnf? Pnanqvnaf? "
    r"pnanqvraf(?:r|rf) Oevgvfu oevgáavp[bn]f? Oevgbaf? Oevgf? Vevfu "
    r"veynaqrf(?:n|rf|nf)? Fpbggvfu Fpbgf rfpbprf(?:n|rf|nf)? Jryfu Fcnavneqf? "
    r"rfcnñbyrf rfcnñbynf Nhfgenyvnaf? nhfgenyvna[bn]f? Vaqvnaf? uvaqúrf "
    r"Yngva_Nzrevpnaf? yngvabnzrevpna[bn]f? uvfcnabnzrevpna[bn]f? Yngvak Yngvabf? "
    r"Yngvanf? yngva[bn]f? ertvbzbagna[bn]f? ertvbf? puvynat[bn]f? gncngí[bn]f? "
    r"abegrñ[bn]f? arbyrbarf(?:n|rf|nf)? Grknaf? grwna[bn]f? Pnyvsbeavnaf? "
    r"pnyvsbeavna[bn]f? puvpnabf? puvpnanf? Zrkvpna-Nzrevpna Rhebcrna rhebcr[bn]f? "
    r"Rhebcrnaf? Nsevpnaf? nsevpna[bn]f? Nfvnaf? nfvágvp[bn]f? Pnevoornaf? "
    r"pnevorñ[bn]f? "
)
# Names that are also ordinary words: matched only when Capitalized.
_CASED = (
    r"HFN RRHH Punq Trbetvn Wbeqna Ghexrl Ghedhín Avtre Aítre Thvarn Puvyr Puvan Znyv Phon "
    r"Znygn Bzna Gbtb Creh Cnanzn Teranqn Fnzbn Gbatn Ohexvan Gevavqnq Pbern Obfavn "
    r"Rzvengbf Pbatb Vaqvn Vfenry Tnyrf Jnyrf Onunznf Ynbf Tnzovn Wrefrl Tenanqn Yróa "
    r"Inyrapvn Yvzn Fnagvntb Ivpgbevn Syberapr Syberapvn Póeqbon Pbeqbon Zéevqn Zrevqn "
    r"Qhenatb Pbyvzn Gnonfpb Fbaben Chroyn Gbyrqb Fnynznapn Flqarl Cnevf Cneíf Ebzn "
    r"Ebzr Orea Orean Qnyynf Ubhfgba Qraire Obfgba Zvnzv Beynaqb Ngynagn Zrzcuvf "
    r"Anfuivyyr Cbegynaq Irtnf Pnapúa Pnapha Gvwhnan Wháerm Whnerm Dhreégneb Dhrergneb "
    r"Puvuhnuhn Pbnuhvyn Wnyvfpb Bnknpn Fvanybn Lhpngáa Lhpngna Mnpngrpnf Gnznhyvcnf "
    r"Irenpehm Threereb Zberybf Uvqnytb Anlnevg Pnzcrpur Puvncnf Gynkpnyn Zvpubnpáa "
    r"Zvpubnpna Thnanwhngb Nthnfpnyvragrf Fnygvyyb Gbeeróa Gbeerba Npnchypb Znmngyáa "
    r"Znmngyna Phyvnpáa Phyvnpna Urezbfvyyb Rafranqn Zrkvpnyv Zberyvn Gbyhpn Cnpuhpn "
    r"Grknf Grwnf Pnyvsbeavn Sybevqn Nevmban Arinqn Hgnu Pbybenqb Bertba Jnfuvatgba "
    r"Zbagnan Qnxbgn Ivetvavn Pnebyvan Buvb Zvpuvtna Vyyvabvf Vaqvnan Nynonzn "
    r"Ybhvfvnan Xraghpxl Graarffrr Zvffbhev Xnafnf Aroenfxn Vbjn Zvaarfbgn Jvfpbafva "
    r"Znvar Irezbag Nynfxn Unjnvv Dhéorp Dhrorp Bagnevb Nyoregn Znavgbon "
)


def _alt(encoded: str) -> str:
    return "|".join(
        codecs.decode(w, "rot13").replace("_", r"\s+")
        for w in sorted(encoded.split(), key=len, reverse=True)
    )


_B, _E = r"(?<![\w])(?:", r")(?![\w])"
_PLACE = re.compile(_B + _alt(_WORDS) + _E, re.IGNORECASE)
_PLACE_CASED = re.compile(_B + _alt(_CASED) + _E)
_FLAG = re.compile("[\U0001f1e6-\U0001f1ff]{2}|\U0001f3f4[\U000e0061-\U000e007a]+")
_LANGS = "es|en|pt|fr|de|it|nl|ca|gl|eu|ja|zh|ko|ru|ar|hi"
# xx-XX is the placeholder the docs use for "any region".
_LOCALE = re.compile(rf"(?<![A-Za-z0-9])(?:{_LANGS})[-_](?!XX)(?:[A-Z]{{2}}|419)(?![A-Za-z0-9])")
_TZ_AREAS = codecs.decode(
    "Nsevpn|Nzrevpn|Nagnepgvpn|Nepgvp|Nfvn|Ngynagvp|Nhfgenyvn|Rhebcr|Vaqvna|Cnpvsvp|HF|Pnanqn|Zrkvpb|Oenmvy|Puvyr",
    "rot13",
)
_TZ = re.compile(rf"\b(?:{_TZ_AREAS})/[A-Z][A-Za-z_]+")

CHECKS = (
    ("place", _PLACE),
    ("place", _PLACE_CASED),
    ("flag", _FLAG),
    ("regional locale", _LOCALE),
    ("place timezone", _TZ),
)


def hits(text: str) -> list[tuple[int, str, str]]:
    """(line number, kind, matched text) for every violation in ``text``."""
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for kind, rx in CHECKS:
            out += [(n, kind, m.group(0)) for m in rx.finditer(line)]
    return out


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout.decode(
        "utf-8", "replace"
    )


def _tree() -> list[str]:
    top = Path(_git("rev-parse", "--show-toplevel").strip())
    found = []
    for rel in _git("ls-files", "-z").split("\0"):
        if not rel:
            continue
        try:
            raw = (top / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in raw[:4096]:
            continue
        found += [f"{rel}:{n}: {k} '{m}'" for n, k, m in hits(raw.decode("utf-8", "replace"))]
    return found


def _staged() -> list[str]:
    found, path, ln = [], "", 0
    for line in _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=AM").splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else ""
        elif line.startswith("@@ "):
            ln = int(line.split()[2].split(",")[0][1:])
        elif line.startswith("+") and path:
            found += [f"{path}:{ln}: {k} '{m}'" for _, k, m in hits(line[1:])]
            ln += 1
    return found


def _messages(text: str, label: str) -> list[str]:
    return [f"{label}:{n}: {k} '{m}'" for n, k, m in hits(text)]


def _history() -> list[str]:
    found = []
    for rec in _git("log", "--format=%h%x00%B%x01").split("\x01"):
        if "\0" in rec:
            sha, body = rec.strip().split("\0", 1)
            found += _messages(body, f"commit {sha}")
    return found


def main(argv: list[str]) -> int:
    mode = argv[1] if len(argv) > 1 else "--tree"
    if mode == "--message":
        msg = Path(argv[2]).read_text(encoding="utf-8", errors="replace")
        body = "\n".join(line for line in msg.splitlines() if not line.startswith("#"))
        found = _messages(body, "commit message")
    else:
        found = {"--tree": _tree, "--staged": _staged, "--history": _history}[mode]()
    for f in found:
        print(f"public copy: {f}")
    if found:
        print(
            "public copy: blocked by AGENTS.md rule 2 (no country, city, region, "
            "nationality, flag, regional locale or place timezone).",
            file=sys.stderr,
        )
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
