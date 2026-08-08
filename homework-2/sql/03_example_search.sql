-- The query behind POST /weather/search. Bind $1 to the 384-dim query vector
-- produced by sentence-transformers (the app passes it as a %s::vector literal).

SELECT d.id,
       d.location,
       d.source_type,
       d.headline,
       e.chunk_text,
       1 - (e.embedding <=> $1::vector) AS similarity
FROM weather_embeddings e
JOIN weather_documents d ON d.id = e.document_id
ORDER BY e.embedding <=> $1::vector
LIMIT 5;
