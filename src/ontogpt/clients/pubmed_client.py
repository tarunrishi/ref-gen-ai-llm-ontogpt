"""Pubmed Client."""

import logging
import time
from dataclasses import dataclass
from typing import Generator, List, Tuple, Union
from urllib import parse

import inflection
import requests
from bs4 import BeautifulSoup
from oaklib.utilities.apikey_manager import get_apikey_value

PMID = str
TITLE_WEIGHT = 5
MAX_PMIDS = 50
RETRY_MAX = 2

PUBMED = "pubmed"
EUTILS_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"


def _normalize(s: str) -> str:
    return inflection.singularize(s).lower()


def _score_paper(paper: str, keywords: List[str]) -> Tuple[PMID, int]:
    """Assign a quality score to a PubMed entry.

    Input needs to be XML so it can be parsed by component.
    :param paper: string, the text of the paper, as XML
    :param keywords: list of keywords to use in scoring
    :return: tuple of article ID (PMID) and score
    """
    # Parse the paper by component first
    soup = BeautifulSoup(paper, "xml")
    for pa in soup.find_all(["PubmedArticle", "PubmedBookArticle"]):  # This should be one exactly
        ti = pa.find("ArticleTitle").text
        pmid = pa.find("ArticleId", IdType="pubmed").text
        if pa.find("Abstract"):  # Document may not have abstract
            ab = pa.find("Abstract").text
        else:
            ab = ""

    title_score = _score_text(ti, keywords)
    abstract_score = _score_text(ab, keywords)
    logging.info(f"Scored {pmid} {ti} with TS={title_score} AS={abstract_score} ")
    score = title_score * TITLE_WEIGHT + abstract_score
    return (pmid, score)


def _score_text(text: str, keywords: List[str]) -> int:
    """Assign a quality score to a text entry.

    A text entry not mentioning any of its keywords has a score of zero.
    Score increases by one for each keyword mentioned in the text.
    :param text: The text to assign the score to
    :param keywords: The keywords to use in scoring
    """
    text = text.lower()
    if not text:
        return -100
    score = 0
    for kw in keywords:
        if kw in text:
            score += 1
    return score


def clean_pmids(ids: list[PMID]) -> list[PMID]:
    """Remove prefixes from a list of PMIDs, returning the new list."""
    clean_ids = [id.replace("PMID:", "", 1) for id in ids]
    return clean_ids


@dataclass
class PubmedClient:
    """A client for the Pubmed API.

    This class is a wrapper around the Entrez API.
    """

    # The maximum length of text, in characters, to include in
    # a single input chunk. This may be set in the CLI
    # with the max_text_length option.
    max_text_length: int = 0

    try:
        email = get_apikey_value("ncbi-email")
    except ValueError:
        email = None
        logging.info("Email for NCBI API not found.")

    try:
        ncbi_key = get_apikey_value("ncbi-key")
    except ValueError:
        ncbi_key = None
        logging.info("NCBI API key not found. Will use no key.")

    def get_pmids(self, term: str) -> List[str]:
        """Search PubMed and retrieve a list of PMIDs matching the search term.

        :param term: The search term to query PubMed.
        :return: A list of PMIDs matching the search term.
        """
        pmids = []

        batch_size = 5000

        search_url = EUTILS_URL + "esearch.fcgi"

        logging.info(f"Finding count of PMIDs matching search term {term}...")
        # If retmax==0, we get only the size of the search result in count of PMIDs
        if self.email and self.ncbi_key:
            params = {
                "db": PUBMED,
                "term": term,
                "retmode": "json",
                "retmax": 0,
                "email": self.email,
                "api_key": self.ncbi_key,
            }
        else:
            params = {"db": PUBMED, "term": term, "retmode": "json", "retmax": 0}

        # We are explicit with the delimiters in this query,
        # no percent encoding allowed. This mostly just makes it more human readable
        response = requests.get(search_url, params=parse.urlencode(params, safe=","))

        if response.status_code == 200:
            data = response.json()
            resultcount = int(data["esearchresult"]["count"])
            logging.info(f"Search returned {resultcount} PMIDs matching search term {term}")
        elif response.status_code == 500:
            logging.error("Internal server error in searching PubMed:", response.status_code)
        elif response.status_code == 429:
            logging.error("Too many requests to NCBI API. Try again later, or use API key.")
        else:
            logging.error("Encountered error in searching PubMed:", response.status_code)

        if resultcount > 9999:
            logging.warning("PubMed limits search results to 9999 records.")
        resultcount = 9999

        # Now we get the list of PMIDs, iterating as needed
        logging.info(f"Retrieving PMIDs matching search term {term}...")
        for retstart in range(0, resultcount, batch_size):
            params["retstart"] = retstart
            params["retmax"] = batch_size

            response = requests.get(search_url, params=parse.urlencode(params, safe=","))

            trying = True
            try_count = 0
            while trying:
                if response.status_code == 200:
                    data = response.json(strict=False)
                    try:
                        these_ids = data["esearchresult"]["idlist"]
                        pmids.extend(these_ids)
                    except KeyError:  # Likely an error message.
                        errortext = data["esearchresult"]["ERROR"]
                        logging.error(f"Response: {errortext}")
                    trying = False
                else:
                    logging.error(f"Encountered error in searching PubMed: {response.status_code}")
                    try_count = try_count + 1
                    if try_count < RETRY_MAX:
                        logging.info("Trying again...")
                        time.sleep(1)
                    else:
                        logging.info(f"Giving up - last status code {response.status_code}")
                        trying = False
        logging.info("Retrieved all PMIDs.")

        return pmids

    def text(
        self, ids: Union[list[PMID], PMID], raw=False, autoformat=True, pubmedcental=False
    ) -> Union[list[str], str]:
        """Get the text of one or more papers from their PMIDs.

        :param ids: List of PubMed IDs, or string with single PMID
        :param raw: if True, do not parse the xml, just return the raw output with tags
        :param autoformat: if True include title and abstract concatenated
        :param pubmedcentral: if True, retrieve text from PubMed Central where possible
        :return: the text of a single entry, or a list of strings for text of multiple entries
        """
        batch_size = 200

        # Check if the PMID(s) can be parsed
        # and remove prefix if present
        if isinstance(ids, PMID):  # If it's a single PMID
            ids = [ids]
            singledoc = True
        else:
            singledoc = False
        clean_ids = clean_pmids(ids)
        ids = clean_ids

        # this will store the document data
        xml_data = ""

        # Check if we have enough IDs to require epost
        # This is stated as being 10000
        # But in practice, the API won't even accept more than ~2500
        # chars worth of IDs, or about 280 to 320 PMIDs.
        # So instead, we iterate.
        use_post = False
        if len(ids) > 275:
            use_post = True

        fetch_url = EUTILS_URL + "efetch.fcgi"

        if not use_post:
            if self.email and self.ncbi_key:
                params = {
                    "db": PUBMED,
                    "id": ",".join(ids),
                    "rettype": "xml",
                    "retmode": "xml",
                    "email": self.email,
                    "api_key": self.ncbi_key,
                }
            else:
                params = {"db": PUBMED, "id": ",".join(ids), "rettype": "xml", "retmode": "xml"}

            for retstart in range(0, len(ids), batch_size):
                params["retstart"] = retstart
                params["retmax"] = batch_size

                response = requests.get(fetch_url, params=parse.urlencode(params, safe=","))

                trying = True
                try_count = 0
                while trying:
                    if response.status_code == 200:
                        xml_data = xml_data + "\n" + response.text
                        trying = False
                    else:
                        logging.error(
                            f"Encountered error in fetching from PubMed: {response.status_code}"
                        )
                        try_count = try_count + 1
                        if try_count < RETRY_MAX:
                            logging.info("Trying again...")
                            time.sleep(1)
                        else:
                            logging.info(f"Giving up - last status code {response.status_code}")
                            trying = False
            logging.info("Retrieved document data.")

        else:
            # Do post request first, iterating as needed
            post_url = EUTILS_URL + "epost.fcgi"
            if self.email and self.ncbi_key:
                params = {
                    "db": PUBMED,
                    "id": "",
                    "email": self.email,
                    "api_key": self.ncbi_key,
                    "WebEnv": "",
                }
            else:
                params = {"db": PUBMED, "id": ",".join(ids), "WebEnv": ""}

            query_keys = []

            # Need to batch the IDs manually for this endpoint
            id_batches = [ids[pmid: pmid + batch_size] for pmid in range(0, len(ids), batch_size)]

            for batch in id_batches:
                params["id"] = ",".join(batch)

                response = requests.post(post_url, params=parse.urlencode(params, safe=","))

                # Get a webenv the first time, then reuse on subsequent requests
                # import pdb; pdb.set_trace()
                if params["WebEnv"] == "":
                    webenv = response.text.split("<WebEnv>")[1].split("</WebEnv>")[0]
                    params["WebEnv"] = webenv
                querykey = response.text.split("<QueryKey>")[1].split("</QueryKey>")[0]
                query_keys.append(querykey)

                trying = True
                try_count = 0
                while trying:
                    if response.status_code == 200:
                        trying = False
                    else:
                        logging.error(
                            f"Encountered error in posting IDs to PubMed: {response.status_code}"
                        )
                        try_count = try_count + 1
                        if try_count < RETRY_MAX:
                            logging.info("Trying again...")
                            time.sleep(0.5)
                        else:
                            logging.info(f"Giving up - last status code {response.status_code}")
                            trying = False
            logging.info(f"Posted PMIDs to WebEnv:{webenv}.")

            # Now do the fetch
            params = {
                "db": PUBMED,
                "query_key": "",
                "WebEnv": webenv,
                "rettype": "xml",
            }

            # Iterate through the query keys we have
            for this_key in query_keys:
                params["query_key"] = this_key

                response = requests.get(fetch_url, params=parse.urlencode(params, safe=","))

                trying = True
                try_count = 0
                while trying:
                    if response.status_code == 200:
                        xml_data = xml_data + "\n" + response.text
                        trying = False
                    else:
                        logging.error(
                            f"Encountered error in fetching from PubMed: {response.status_code}"
                        )
                        try_count = try_count + 1
                        if try_count < RETRY_MAX:
                            logging.info("Trying again...")
                            time.sleep(0.5)
                        else:
                            logging.info(f"Giving up - last status code {response.status_code}")
                            trying = False
            logging.info("Retrieved document data.")

        # Parse that xml - this returns a list of strings
        # if raw is True, the tags are kept, but we still get a list of docs
        # and we don't truncate them
        these_docs = self.parse_pmxml(
            xml=xml_data, raw=raw, autoformat=autoformat, pubmedcentral=pubmedcental
        )

        txt = []
        onetxt = ""
        for doc in these_docs:
            if self.max_text_length > 0:
                if len(doc) > self.max_text_length and not raw:
                    charnum = str(self.max_text_length)
                    logging.warning(f'Truncating entry beginning "{doc[:50]}" to {charnum} chars')
                    shortdoc = doc[0: self.max_text_length]
                    txt.append(shortdoc)
                else:
                    txt.append(doc)
            else:
                txt.append(doc)
            if singledoc and not pubmedcental:
                onetxt = txt[0]

        if len(onetxt) > 0:
            return onetxt
        else:
            return txt

    def pmc_text(self, pmc_id: str) -> str:
        """Get the text of one PubMed Central entry.

        Don't parse further here - just get the raw response.
        :param pmc_id: List of PubMed IDs, or string with single PMID
        :return: the text of a single entry as XML
        """
        xml_data = ""

        fetch_url = EUTILS_URL + "efetch.fcgi"

        if self.email and self.ncbi_key:
            params = {
                "db": "pmc",
                "id": pmc_id,
                "rettype": "xml",
                "retmode": "xml",
                "email": self.email,
                "api_key": self.ncbi_key,
            }
        else:
            params = {"db": "pmc", "id": pmc_id, "rettype": "xml", "retmode": "xml"}

        response = requests.get(fetch_url, params=parse.urlencode(params, safe=","))

        trying = True
        try_count = 0
        while trying:
            if response.status_code == 200:
                xml_data = response.text
                trying = False
            else:
                logging.error(
                    f"Encountered error in fetching from PubMed Central: {response.status_code}"
                )
                try_count = try_count + 1
                if try_count < RETRY_MAX:
                    logging.info("Trying again...")
                    time.sleep(1)
                else:
                    logging.info(f"Giving up - last status code {response.status_code}")
                    trying = False
        logging.info(f"Retrieved PubMed Central document data for {pmc_id}.")

        return xml_data

    def search(self, term: str, keywords: List[str]) -> Generator[PMID, None, None]:
        """Get the quality-scored text of PubMed papers relating to a search term and keywords.

        This generator yields PMIDs. Note this uses the MAX_PMIDS value
        to determine how many documents to collect.
        :param term: search term, a string
        :param keywords: keywords, a list of strings
        :return: PMIDs corresponding to the search term and keywords
        """
        if keywords:
            keywords = [_normalize(kw) for kw in keywords]
            term = f"({term}) AND ({' OR '.join(keywords)})"
        else:
            keywords = []

        logging.info(f"Searching for {term}...")

        esr = self.get_pmids(term=term)

        paset = self.text(ids=esr[0:MAX_PMIDS], raw=True)

        scored_papers = [(_score_paper(paper, keywords), paper) for paper in paset]
        scored_papers.sort(key=lambda x: x[1][0], reverse=True)

        for id_and_score, _paper in scored_papers:
            pmid = id_and_score[0]
            score = id_and_score[1]
            logging.debug(f"Yielding {pmid} with score {score} ")
            yield f"{pmid}"

    def parse_pmxml(self, xml: str, raw: bool, autoformat: bool, pubmedcentral: bool) -> List[str]:
        """Extract structured text from PubMed and PubMed Central XML.

        :param xml: One or more xml entries, as string
        :param raw: if True, do not parse the xml beyond separating documents
        :param autoformat: if True include title and abstract concatenated
            Otherwise the output will include ALL text contents besides XML tags
        :param pubmedcentral: if True replace abstract with PubMed Central text
            If there isn't a PMC ID, just use the abstract.
            If there is a PMC ID, use the abstract AND chunk the body text.
            This means the same ID may have multiple entries and may require multiple
            queries to the LLM.
        :return: a list of strings, one per entry
        """
        docs = []

        # Preprocess the string to ensure it's valid xml
        if not raw:
            logging.info("Preprocessing all xml entries...")
            header = "\n".join(xml.split("\n", 3)[0:3])
            pmas_opener = "<PubmedArticleSet>"
            pmas_closer = "</PubmedArticleSet>"
            for remove_string in [header, pmas_opener, pmas_closer]:
                xml = xml.replace(remove_string, "\n")
            xml = pmas_opener + xml + pmas_closer

        soup = BeautifulSoup(xml, "xml")

        logging.info("Parsing all xml entries...")
        for pa in soup.find_all(["PubmedArticle", "PubmedBookArticle"]):
            # First check the PMID, and if requested, any PMC ID
            pmid = ""
            if pa.find("PMID"):  # If this is missing something has gone Wrong
                pmid = pa.find("PMID").text
            pmc_id = ""
            has_pmc_id = False
            try:  # There's a chance that this entry is missing one or more fields below
                if (
                    pa.find("PubmedData").find("ArticleIdList").find("ArticleId", {"IdType": "pmc"})
                    and pubmedcentral
                ):
                    pmc_id = (
                        pa.find("PubmedData")
                        .find("ArticleIdList")
                        .find("ArticleId", {"IdType": "pmc"})
                        .text
                    )
                    has_pmc_id = True
            except AttributeError:
                logging.info(f"PubMed entry {pmid} is missing the expected PubMedData fields.")
            if autoformat and not raw and not has_pmc_id:  # No PMC ID - just use title+abstract
                ti = ""
                if pa.find("ArticleTitle"):
                    ti = pa.find("ArticleTitle").text
                ab = ""
                if pa.find("Abstract"):  # Document may not have abstract
                    ab = pa.find("Abstract").text
                kw = [""]
                if pa.find("KeywordList"):  # Document may not have MeSH terms or keywords
                    kw = [tag.text for tag in pa.find_all("Keyword")]
                txt = f"Title: {ti}\nKeywords: {'; '.join(kw)}\nPMID: {pmid}\nAbstract: {ab}"
                docs.append(txt)
            elif autoformat and not raw and has_pmc_id:  # PMC ID - get and use that text instead
                fulltext = self.pmc_text(pmc_id)
                fullsoup = BeautifulSoup(fulltext, "xml")
                body = ""
                if fullsoup.find("pmc-articleset").find("article").find("body"):
                    body = fullsoup.find("pmc-articleset").find("article").find("body").text
                    body = body.replace("\n", " ")
                ti = ""
                if pa.find("ArticleTitle"):
                    ti = pa.find("ArticleTitle").text
                if pa.find("Abstract"):  # Document may not have abstract
                    body = pa.find("Abstract").text + body
                kw = [""]
                if pa.find("KeywordList"):  # Document may not have MeSH terms or keywords
                    kw = [tag.text for tag in pa.find_all("Keyword")]

                id_txt = f"Title: {ti}\nKeywords: {'; '.join(kw)}\nPMID: {pmid}\nPMCID: {pmc_id}\n"
                full_max_len = self.max_text_length - len(id_txt)
                chunktxt = [body[i: i + full_max_len] for i in range(0, len(body), full_max_len)]
                for txt in chunktxt:
                    docs.append(id_txt + txt)
                    logging.warning(
                        f'Truncating entry containing "{txt[:50]}" to {self.max_text_length} chars'
                    )
            elif raw:
                docs.append(str(pa))
            else:
                docs.append(soup.get_text())

        return docs
