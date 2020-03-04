#
#  Copyright (c) 2020 IBM Corp.
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

################################################################################
# io.py
#
# Functions in text_extensions_for_pandas that create DataFrames and convert
# them to other formats.

import numpy as np
import pandas as pd
import textwrap
import json

import spacy
import spacy.tokens.doc
from spacy.tokenizer import Tokenizer
from spacy.lang.en import English

from text_extensions_for_pandas.char_span import CharSpan, CharSpanType, CharSpanArray
from text_extensions_for_pandas.token_span import TokenSpan, TokenSpanType, TokenSpanArray

# Set to True to use sparse storage for tokens 2-n of n-token dictionary
# entries. First token is always stored dense, of course.
# Currently set to False to avoid spurious Pandas API warnings about conversion
# from sparse to dense.
# TODO: Turn this back on when Pandas fixes the issue with the warning.
_SPARSE_DICT_ENTRIES = False


def make_tokens(target_text: str,
                tokenizer: spacy.tokenizer.Tokenizer) -> pd.Series:
    """
    :param target_text: Text to tokenize
    :param tokenizer: Preconfigured tokenizer object
    :return: The tokens (and underlying text) as a Pandas Series wrapped around
        a `CharSpanArray` value.
    """
    spacy_doc = tokenizer(target_text)
    tok_begins = np.array([t.idx for t in spacy_doc])
    tok_ends = np.array([t.idx + len(t) for t in spacy_doc])
    return pd.Series(CharSpanArray(target_text, tok_begins, tok_ends))


def make_tokens_and_features(target_text: str,
                             language_model: spacy.language.Language,
                             add_left_and_right=False) -> pd.DataFrame:
    """
    :param target_text: Text to analyze

    :param language_model: Preconfigured spaCy language model object

    :param add_left_and_right: If `True`, add columns "left" and "right"
    containing references to previous and next tokens.

    :return: A tuple of two dataframes:
    1. The tokens of the text plus additional linguistic features that the
       language model generates, represented as a `pd.DataFrame`.
    2. A table of named entities identified by the language model's named entity
       tagger, represented as a `pd.DataFrame`.
    """
    spacy_doc = language_model(target_text)

    # TODO: Performance tuning of the translation code that follows
    # Represent the character spans of the tokens
    tok_begins = np.array([t.idx for t in spacy_doc])
    tok_ends = np.array([t.idx + len(t) for t in spacy_doc])
    tokens_array = CharSpanArray(target_text, tok_begins, tok_ends)
    tokens_series = pd.Series(tokens_array)
    # Also build token-based spans to make it easier to compose
    token_spans = TokenSpanArray.from_char_offsets(tokens_series.values)
    # spaCy identifies tokens by semi-arbitrary integer "indexes" (in practice,
    # the offset of the first character in the token). Translate from these
    # to a dense range of integer IDs that will correspond to the index of our
    # returned DataFrame.
    idx_to_id = {spacy_doc[i].idx: i for i in range(len(spacy_doc))}
    df_cols = {
        "id": range(len(tok_begins)),
        "char_span": tokens_series,
        "token_span": token_spans,
        "lemma": [t.lemma_ for t in spacy_doc],
        "pos": pd.Categorical([str(t.pos_) for t in spacy_doc]),
        "tag": pd.Categorical([str(t.tag_) for t in spacy_doc]),
        "dep": pd.Categorical([str(t.dep_) for t in spacy_doc]),
        "head": np.array([idx_to_id[t.head.idx] for t in spacy_doc]),
        "shape": pd.Categorical([t.shape_ for t in spacy_doc]),
        "ent_iob": pd.Categorical([str(t.ent_iob_) for t in spacy_doc]),
        "ent_type": pd.Categorical([str(t.ent_type_) for t in spacy_doc]),
        "is_alpha": np.array([t.is_alpha for t in spacy_doc]),
        "is_stop": np.array([t.is_stop for t in spacy_doc]),
        "sentence": _make_sentences_series(spacy_doc, tokens_array)
    }
    if add_left_and_right:
        # Use nullable int type because these columns contain nulls
        df_cols["left"] = pd.array(
            [None] + list(range(len(tok_begins) - 1)), dtype=pd.Int32Dtype()
        )
        df_cols["right"] = pd.array(
            list(range(1, len(tok_begins))) + [None], dtype=pd.Int32Dtype()
        )
    return pd.DataFrame(df_cols)


def _make_sentences_series(spacy_doc: spacy.tokens.doc.Doc,
                           tokens: CharSpanArray):
    """
    Subroutine of `make_tokens_and_features()`

    :param spacy_doc: parsed document from a spaCy language model

    :param tokens: Token information for the current document as a
    `CharSpanArray` object. Must contain the same tokens as `spacy_doc`.

    :return: a Pandas DataFrame Series containing the token span of the (single)
    sentence that the token is in
    """
    num_toks = len(spacy_doc)
    # Generate the [begin, end) intervals that make up a series of spans
    begin_tokens = np.full(shape=num_toks, fill_value=-1, dtype=np.int)
    end_tokens = np.full(shape=num_toks, fill_value=-1, dtype=np.int)
    for sent in spacy_doc.sents:
        begin_tokens[sent.start:sent.end] = sent.start
        end_tokens[sent.start:sent.end] = sent.end
    return pd.Series(TokenSpanArray(tokens, begin_tokens, end_tokens))


def token_features_to_tree(token_features: pd.DataFrame,
                           text_col: str = "token_span",
                           tag_col: str = "tag",
                           label_col: str = "dep"):
    """
    Convert a DataFrame in the format returned by `make_tokens_and_features()`
    to the public input format of displaCy's dependency tree renderer.

    :param token_features: A subset of a token features DataFrame in the format
    returned by `make_tokens_and_features()`. Must at a minimum contain the
    `head` column and an integer index that corresponds to the ints
    in the `head` column.

    :param text_col: Name of the column in `token_features` from which the
    'covered text' label for each node of the parse tree should be extracted,
    or `None` to leave those labels blank.

    :param tag_col: Name of the column in `token_features` from which the
    'tag' label for each node of the parse tree should be extracted; or `None`
    to leave those labels blank.

    :param label_col: Name of the column in `token_features` from which the
    label for each edge of the parse tree should be extracted; or `None`
    to leave those labels blank.

    :returns: Native Python type representation of the parse tree in a format
    suitable to pass to `displacy.render(manual=True ...)`
    See https://spacy.io/usage/visualizers for the specification of this format.
    """

    # displaCy expects most inputs as strings. Centralize this conversion.
    def _get_text(col_name):
        if col_name is None:
            return np.zeros(shape=len(token_features.index), dtype=str)
        series = token_features[col_name]
        if isinstance(series.dtype, (CharSpanType, TokenSpanType)):
            return series.values.covered_text
        else:
            return series.astype(str)

    # Renumber the head column to a dense range starting from zero
    tok_map = {token_features.index[i]: i
               for i in range(len(token_features.index))}
    # Note that we turn any links to tokens not in our input rows into
    # self-links, which will get removed later on.
    head_tok = token_features["head"].values
    remapped_head_tok = []
    for i in range(len(token_features.index)):
        remapped_head_tok.append(
            tok_map[head_tok[i]] if head_tok[i] in tok_map
            else i
        )

    words_df = pd.DataFrame({
        "text": _get_text(text_col),
        "tag": _get_text(tag_col)
    })
    edges_df = pd.DataFrame({
        "from": range(len(token_features.index)),
        "to": remapped_head_tok,
        "label": _get_text(label_col),
    })
    # displaCy requires all arcs to have their start and end be in
    # numeric order. An additional attribute "dir" tells which way
    # (left or right) each arc goes.
    arcs_df = pd.DataFrame({
        "start": edges_df[["from", "to"]].min(axis=1),
        "end": edges_df[["from", "to"]].max(axis=1),
        "label": edges_df["label"],
        "dir": "left"
    })
    arcs_df["dir"].mask(edges_df["from"] > edges_df["to"], "right",
                        inplace=True)

    # Don't render self-links
    arcs_df = arcs_df[arcs_df["start"] != arcs_df["end"]]

    return {
        "words": words_df.to_dict(orient="records"),
        "arcs": arcs_df.to_dict(orient="records")
    }


def iob_to_spans(token_features: pd.DataFrame,
                 iob_col_name: str = "ent_iob",
                 char_span_col_name: str = "char_span",
                 entity_type_col_name: str = "ent_type"):
    """
    Convert token tags in Inside–Outside–Beginning (IOB) format to a series of
    `TokenSpan`s of entities.

    :param token_features: DataFrame of token features in the format returned by
     `make_tokens_and_features`.

    :param iob_col_name: Name of a column in `token_features` that contains the
     IOB tags as strings, "I", "O", or "B".

    :param char_span_col_name: Name of a column in `token_features` that
     contains the tokens as a `CharSpanArray`.

    :param entity_type_col_name: Optional name of a column in `token_features`
     that contains entity type information; or `None` if no such column exists.

    :return: A `pd.DataFrame` with the following columns:
    * `token_span`: Span (with token offsets) of each entity
    * `<value of entity_type_col_name>`: (optional) Entity type
    """
    # Start out with 1-token prefixes of all entities.
    begin_mask = token_features[iob_col_name] == "B"
    first_tokens = token_features[begin_mask].index
    entity_types = token_features[begin_mask]["ent_type"]
    entity_prefixes = pd.DataFrame({
        "ent_type": entity_types,
        "begin": first_tokens,  # Inclusive
        "end": first_tokens + 1,  # Exclusive
        "next_tag": token_features.iloc[first_tokens + 1][iob_col_name].values
    })

    df_list = []  # Type: pd.DataFrame

    if len(entity_prefixes.index) == 0:
        # Code below needs at least one element in the list for schema
        df_list = [entity_prefixes]

    # Iteratively expand the prefixes
    while len(entity_prefixes.index) > 0:
        complete_mask = entity_prefixes["next_tag"].isin(["O", "B"])
        complete_entities = entity_prefixes[complete_mask]
        incomplete_entities = entity_prefixes[~complete_mask].copy()
        incomplete_entities["end"] = incomplete_entities["end"] + 1
        incomplete_entities["next_tag"] = \
            token_features.iloc[incomplete_entities["end"]][iob_col_name].values
        df_list.append(complete_entities)
        entity_prefixes = incomplete_entities
    all_entities = pd.concat(df_list)

    # Sort spans by location, not length.
    all_entities.sort_values("begin", inplace=True)

    # Convert [begin, end) pairs to spans
    entity_spans_array = (
        TokenSpanArray(token_features[char_span_col_name].values,
                       all_entities["begin"].values,
                       all_entities["end"].values))
    if entity_type_col_name is None:
        return pd.DataFrame({"token_span": entity_spans_array})
    else:
        return pd.DataFrame({
            "token_span": entity_spans_array,
            entity_type_col_name: all_entities["ent_type"].values
        })


def render_parse_tree(token_features: pd.DataFrame,
                      text_col: str = "token_span",
                      tag_col: str = "tag",
                      label_col: str = "dep"):
    """
    Display a DataFrame in the format returned by `make_tokens_and_features()`
    using displaCy's dependency tree renderer.

    :param token_features: A subset of a token features DataFrame in the format
    returned by `make_tokens_and_features()`. Must at a minimum contain the
    `head` column and an integer index that corresponds to the ints
    in the `head` column.

    :param text_col: Name of the column in `token_features` from which the
    'covered text' label for each node of the parse tree should be extracted,
    or `None` to leave those labels blank.

    :param tag_col: Name of the column in `token_features` from which the
    'tag' label for each node of the parse tree should be extracted; or `None`
    to leave those labels blank.

    :param label_col: Name of the column in `token_features` from which the
    label for each edge of the parse tree should be extracted; or `None`
    to leave those labels blank.

    :returns: Native Python type representation of the parse tree in a format
    suitable to pass to `displacy.render(manual=True ...)`
    See https://spacy.io/usage/visualizers for the specification of this format.
    """
    return spacy.displacy.render(token_features_to_tree(token_features,
                                                        text_col, tag_col,
                                                        label_col),
                                 manual=True)


def load_dict(file_name: str, tokenizer: spacy.tokenizer.Tokenizer):
    """
    Load a SystemT-format dictionary file. File format is one entry per line.

    Tokenizes and normalizes the dictionary entries.

    :param file_name: Path to dictionary file

    :param tokenizer: Preconfigured tokenizer object for tokenizing
    dictionary entries.  **Must be the same configuration as the tokenizer
    used on the target text!**

    :return: a `pd.DataFrame` with the normalized entries.
    """
    with open(file_name, "r") as f:
        lines = [line.strip() for line in f.readlines() if len(line) > 0
                 and line[0] != "#"]

    # Tokenize with SpaCy. Produces a SpaCy document object per line.
    tokenized_entries = [tokenizer(line.lower()) for line in lines]

    # Determine the number of tokens in the longest dictionary entry.
    max_num_toks = max([len(e) for e in tokenized_entries])

    # Generate a column for each token. Go one past the max number of tokens so
    # that every dictionary entry ends up None-terminated.
    cols_dict = {}
    for i in range(max_num_toks + 1):
        # Extract token i from every entry that has a token i
        toks_list = [e[i].text if len(e) > i else None for e in
                     tokenized_entries]
        cols_dict["toks_{}".format(i)] = (
            # Sparse storage for tokens 2 and onward
            toks_list if i == 0 or not _SPARSE_DICT_ENTRIES
            else pd.SparseArray(toks_list)
        )

    return pd.DataFrame(cols_dict)
