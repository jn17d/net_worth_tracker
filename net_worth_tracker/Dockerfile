ARG BUILD_FROM
FROM ${BUILD_FROM}

# The app is pure Python 3 standard library — nothing to install.

COPY run.sh /
RUN chmod a+x /run.sh

COPY app /app

WORKDIR /app
CMD [ "/run.sh" ]
